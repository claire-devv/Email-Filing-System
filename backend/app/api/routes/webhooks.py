import asyncio
import base64
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import FilingLog, GmailWatchState
from app.db.session import SessionLocal, get_db
from app.services.gmail_service import GmailService
from app.services.processing_service import ProcessingService
from app.utils.time import utc_now

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


@router.post("/gmail/pubsub")
async def gmail_pubsub(
    payload: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """
    Acknowledge Google Pub/Sub immediately and process emails in the background.

    Google retries if the endpoint doesn't respond within ~30 s. Processing an email
    (WeasyPrint PDF + Claude API) routinely takes 15-60 s, so we must return 200 fast
    and do the heavy work asynchronously.

    Concurrency safety: the history cursor is advanced inside the synchronous request
    handler (before the 200 is returned) so that a burst of simultaneous Pub/Sub
    notifications each start from a different cursor snapshot and never re-fetch the
    same message batch.
    """
    try:
        data = payload.get("message", {}).get("data")
        decoded = _decode_pubsub_data(data)
        email_address = decoded.get("emailAddress")
        notification_history_id = str(decoded.get("historyId") or "")

        if not notification_history_id:
            return {"status": "ignored", "reason": "No Gmail historyId in Pub/Sub payload.", "decoded": decoded}

        state = _watch_state(db, email_address)
        if not state:
            return {
                "status": "ignored",
                "reason": "No Gmail watch state. Start a watch before processing Pub/Sub notifications.",
                "email_address": email_address,
                "history_id": notification_history_id,
            }

        state.last_notification_at = utc_now()

        if not state.history_id:
            # First notification ever: store the cursor; there is no prior range to fetch.
            state.history_id = notification_history_id
            state.last_successful_sync_at = utc_now()
            state.last_error = None
            db.add(state)
            db.commit()
            return {
                "status": "initialized",
                "reason": "Stored initial Gmail historyId; no prior history cursor was available.",
                "email_address": email_address,
                "history_id": notification_history_id,
            }

        # Advance cursor NOW (before returning 200) so concurrent notifications each get
        # a unique snapshot of where to start. The background task reads history from
        # cursor_start; if it fails, the cursor is already past those messages — any
        # unprocessed emails will be caught by the next notification or the safety-net
        # / retry loops.
        cursor_start = state.history_id
        state.history_id = notification_history_id
        db.add(state)
        db.commit()

        background_tasks.add_task(
            _process_notification_in_background,
            cursor_start=cursor_start,
            notification_history_id=notification_history_id,
            email_address=email_address,
            watch_state_id=state.id,
        )
        return {"status": "accepted", "cursor_start": cursor_start}

    except Exception as exc:
        logger.exception("Pub/Sub webhook error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _process_notification_in_background(
    cursor_start: str,
    notification_history_id: str,
    email_address: str | None,
    watch_state_id: int,
) -> None:
    """Run in the background after the HTTP 200 has been sent to Google."""
    db = SessionLocal()
    try:
        await asyncio.to_thread(
            _process_notification_sync,
            db,
            cursor_start,
            notification_history_id,
            email_address,
            watch_state_id,
        )
    except Exception:
        logger.exception("Background Pub/Sub processing failed (cursor_start=%s)", cursor_start)
    finally:
        db.close()


def _process_notification_sync(
    db: Session,
    cursor_start: str,
    notification_history_id: str,
    email_address: str | None,
    watch_state_id: int,
) -> None:
    state = db.get(GmailWatchState, watch_state_id)
    if not state:
        logger.warning("Watch state %s missing; skipping background processing", watch_state_id)
        return

    try:
        message_ids, latest_history_id = GmailService().history_message_ids(
            cursor_start,
            label_ids=state.label_ids or get_settings().gmail_pubsub_label_ids,
        )
    except HttpError as exc:
        if getattr(exc.resp, "status", None) in {404, 410}:
            _fallback_to_unread_sync(db, state, notification_history_id, str(exc))
            return
        state.last_error = f"Gmail history API error: {exc}"
        db.add(state)
        db.commit()
        logger.error("Gmail history API error (cursor=%s): %s", cursor_start, exc)
        return

    # Advance the cursor to the latest history ID seen in the API response. This is the
    # authoritative new position — prefer it over the notification_history_id (which is
    # just the ID attached to the push message and may lag the actual messages fetched).
    new_cursor = latest_history_id or notification_history_id
    if new_cursor and new_cursor != state.history_id:
        state.history_id = new_cursor
        db.add(state)
        db.commit()

    logger.info(
        "Pub/Sub batch: %d message(s) from cursor %s → %s",
        len(message_ids),
        cursor_start,
        new_cursor,
    )

    if not message_ids:
        state.last_successful_sync_at = utc_now()
        state.last_error = None
        db.add(state)
        db.commit()
        return

    processor = ProcessingService()
    failed_count = 0
    for message_id in message_ids:
        try:
            result = processor.process_message(db, message_id)
            logger.info("Processed %s → %s", message_id, result.get("status"))
        except Exception:
            failed_count += 1
            logger.exception("Failed to process message %s", message_id)

    state.last_successful_sync_at = utc_now()
    state.last_error = f"{failed_count} message(s) failed during Pub/Sub processing." if failed_count else None
    db.add(state)
    db.commit()


def _decode_pubsub_data(data: str | None) -> dict:
    if not data:
        return {}
    padded = data + "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("utf-8"))
    return json.loads(raw.decode("utf-8"))


def _watch_state(db: Session, email_address: str | None) -> GmailWatchState | None:
    if email_address:
        exact = db.execute(
            select(GmailWatchState).where(GmailWatchState.email_address == email_address)
        ).scalars().first()
        if exact:
            return exact
    return db.execute(
        select(GmailWatchState)
        .where(GmailWatchState.active.is_(True))
        .order_by(GmailWatchState.updated_at.desc())
    ).scalars().first()


def _fallback_to_unread_sync(db: Session, state: GmailWatchState, notification_history_id: str, reason: str) -> dict:
    result = ProcessingService().process_unread(db, get_settings().process_unread_max_limit, newer_than_minutes=None)
    state.history_id = notification_history_id
    state.last_successful_sync_at = utc_now()
    state.last_error = f"Gmail history cursor was unavailable; fell back to unread sync. Reason: {reason}"
    db.add(
        FilingLog(
            status="system_warning",
            message=state.last_error,
        )
    )
    db.add(state)
    db.commit()
    return {
        "status": "fallback_processed",
        "reason": "Gmail history cursor expired or was unavailable; processed unread messages and reset cursor.",
        "history_id": notification_history_id,
        "result": result,
    }

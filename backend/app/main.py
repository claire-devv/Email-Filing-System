import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import activity, admin, auth, documents, emails, entities, google, health, notifications, review, users, webhooks
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import get_current_user, require_admin
from app.db.session import SessionLocal, init_db
from app.services import watch_service
from app.services.processing_service import MAX_AUTO_RETRY_ATTEMPTS as _MAX_AUTO_RETRY_ATTEMPTS
from app.services.rulebook_service import RulebookService
from app.utils.time import utc_now


logger = logging.getLogger(__name__)

# If a watch-renewal fails, retry sooner than the full interval so the ~7-day
# watch never lapses due to a single transient Google error.
_WATCH_RENEW_RETRY_SECONDS = 3600

# How long to wait between automatic retries of failed / api-limited emails.
_RETRY_INTERVAL_SECONDS = 5 * 60  # 5 minutes

# Safety-net: re-scan the Gmail inbox this often even if Pub/Sub is working,
# to catch any messages that slipped through during brief outages or cold starts.
_SAFETY_NET_INTERVAL_SECONDS = 30 * 60  # 30 minutes

# Emails that hit the API daily limit are retried after this delay, giving the
# limit time to reset before hammering the quota again.
_API_LIMIT_RETRY_AFTER = timedelta(hours=2)

# Give up automatic retries after this many attempts; an operator must inspect.
# (imported above from processing_service so main.py, admin.py's /failed-emails diagnostic
# endpoint, and the retry loop below all agree on the same ceiling)


# ---------------------------------------------------------------------------
# Gmail watch renewal
# ---------------------------------------------------------------------------

def _renew_watch_once() -> bool:
    """Re-watch Gmail so Pub/Sub push notifications never lapse. Best-effort."""
    settings = get_settings()
    if not settings.enable_real_google or not settings.gmail_pubsub_topic_name:
        return True
    db = SessionLocal()
    try:
        state = watch_service.renew_watch(db)
        logger.info("Gmail watch renewed; expires %s", state.expiration_at)
        return True
    except Exception as exc:
        logger.exception("Gmail watch auto-renewal failed; will retry in %ds", _WATCH_RENEW_RETRY_SECONDS)
        _record_renewal_error(db, str(exc))
        return False
    finally:
        db.close()


def _record_renewal_error(db: Session, message: str) -> None:
    from app.db.models import GmailWatchState
    with contextlib.suppress(Exception):
        db.rollback()
        state = db.execute(
            select(GmailWatchState).order_by(GmailWatchState.updated_at.desc())
        ).scalars().first()
        if state:
            state.last_error = f"Auto-renewal failed: {message}"
            db.add(state)
            db.commit()


async def _watch_renewal_loop(interval_seconds: int) -> None:
    while True:
        ok = await asyncio.to_thread(_renew_watch_once)
        await asyncio.sleep(interval_seconds if ok else _WATCH_RENEW_RETRY_SECONDS)


# ---------------------------------------------------------------------------
# Failed-email retry loop
# ---------------------------------------------------------------------------

def _retry_failed_emails_once() -> None:
    """
    Retry emails that failed or were parked due to an API limit.

    Selection rules:
    - "failed": retry after 5 min, up to _MAX_AUTO_RETRY_ATTEMPTS attempts.
    - "waiting_api_limit": retry after 2 h so the daily quota has time to reset.
    Both are skipped once they hit the attempt ceiling (manual intervention needed).
    """
    settings = get_settings()
    if not settings.enable_real_google:
        return
    # Local testing: skip all auto-retries so a parked backlog (failed / waiting_api_limit)
    # never re-spends Claude calls. New emails still process via the webhook / Run now.
    if settings.process_new_only:
        return

    from app.db.models import ProcessedEmail
    from app.services.processing_service import ProcessingService

    db = SessionLocal()
    try:
        now = utc_now()
        failed_cutoff = now - timedelta(minutes=5)
        api_limit_cutoff = now - _API_LIMIT_RETRY_AFTER

        rows = db.execute(
            select(ProcessedEmail)
            .where(
                # Drive-upload rows are retried by _retry_failed_uploads_once, NOT here: this loop
                # calls process_message -> gmail.fetch_message, which can't fetch a synthetic
                # "drive-upload:<id>" and would fail forever.
                ProcessedEmail.gmail_message_id.not_like("drive-upload:%"),
                (
                    # 'failed' = a problem with THIS email; give up after N attempts (further
                    # retries would just fail the same way and burn API calls for nothing).
                    (ProcessedEmail.status == "failed")
                    & (ProcessedEmail.updated_at < failed_cutoff)
                    & (ProcessedEmail.attempts < _MAX_AUTO_RETRY_ATTEMPTS)
                ) | (
                    # 'waiting_api_limit' = an ACCOUNT/API-level block (our own daily cap, or Claude
                    # being rate-limited/down/out of credits) -- not this email's fault, and it
                    # resolves itself once Anthropic/billing recovers. No attempt ceiling: an
                    # hours-long outage would otherwise permanently strand every email queued during
                    # it, needing a manual reset to recover (see backend/app/utils/errors.py:
                    # is_api_unavailable_error).
                    (ProcessedEmail.status == "waiting_api_limit") & (ProcessedEmail.updated_at < api_limit_cutoff)
                ),
            )
            .order_by(ProcessedEmail.updated_at.asc())
            .limit(5)
        ).scalars().all()

        if not rows:
            return

        processor = ProcessingService()
        for email_row in rows:
            # Skip permanent failures (e.g. password-protected/corrupt attachment) — retrying
            # always fails the same way. They stay 'failed' with a clear message for a human.
            if (email_row.metadata_json or {}).get("permanent_failure"):
                continue
            try:
                result = processor.process_message(db, email_row.gmail_message_id)
                logger.info(
                    "Auto-retry %s (attempt %d) → %s",
                    email_row.gmail_message_id,
                    email_row.attempts,
                    result.get("status"),
                )
            except Exception:
                logger.exception("Auto-retry failed for %s", email_row.gmail_message_id)
    except Exception:
        logger.exception("Retry loop encountered an unexpected error")
    finally:
        db.close()


async def _retry_loop() -> None:
    while True:
        await asyncio.sleep(_RETRY_INTERVAL_SECONDS)
        await asyncio.to_thread(_retry_failed_emails_once)


# ---------------------------------------------------------------------------
# Safety-net inbox scan
# ---------------------------------------------------------------------------

def _safety_net_scan_once() -> None:
    """
    Scan for unread inbox emails that Pub/Sub may have missed (e.g. server was
    down when a notification arrived, or watch expired briefly before renewal).
    This is a last-resort net — it does NOT replace Pub/Sub; it complements it.
    """
    settings = get_settings()
    if not settings.enable_real_google:
        return

    from app.services.processing_service import ProcessingService

    db = SessionLocal()
    try:
        result = ProcessingService().process_unread(
            db,
            limit=settings.process_unread_max_limit,
            newer_than_minutes=None,
        )
        processed = result.get("processed_count", 0)
        if processed:
            logger.info("Safety-net scan: processed %d unread email(s)", processed)
    except Exception:
        logger.exception("Safety-net scan failed")
    finally:
        db.close()


async def _safety_net_loop() -> None:
    # Wait one full interval before the first run so it doesn't fire at startup
    # when Pub/Sub is already processing any backlog.
    await asyncio.sleep(_SAFETY_NET_INTERVAL_SECONDS)
    while True:
        await asyncio.to_thread(_safety_net_scan_once)
        await asyncio.sleep(_SAFETY_NET_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Drive upload-folder scanning ("Client Uploads" + "RRES Uploads")
# ---------------------------------------------------------------------------

def _scan_uploads_once() -> None:
    """
    Scan the Drive upload folders and file any new files. Discovery is two drive-wide queries
    (all "Client Uploads" folders + the one "RRES Uploads" folder) regardless of entity count.
    See backend/DRIVE_UPLOADS_PLAN.md.
    """
    settings = get_settings()
    if not (settings.enable_real_google and settings.drive_root_id and settings.uploads_scan_enabled):
        return

    from app.services.drive_service import DriveService
    from app.services.upload_ingest_service import UploadIngestService

    db = SessionLocal()
    try:
        drive = DriveService()
        ingest = UploadIngestService()
        found = drive.find_upload_folders()

        # Map each "Client Uploads" folder to its owning entity via its parent id.
        from app.services.entity_service import EntityService

        by_folder_id = {e.drive_folder_id: e for e in EntityService().list_active(db) if e.drive_folder_id}
        jobs: list[tuple[dict, str, str, str | None]] = []
        for folder in found.get("client_uploads") or []:
            owner = next((by_folder_id.get(p) for p in (folder.get("parents") or []) if p in by_folder_id), None)
            if not owner:
                continue  # a stray "Client Uploads" folder not under a known entity -> skip
            jobs.append((folder, folder["id"], "client_uploads", owner.entity_name))
        rres = found.get("rres_uploads")
        if rres:
            jobs.append((rres, rres["id"], "rres_uploads", None))

        for folder, folder_id, source_kind, fixed_entity in jobs:
            try:
                files = drive.list_files_in_folder(folder_id)
            except Exception:
                logger.exception("Upload scan: listing %s failed", folder_id)
                continue
            for file_meta in files:
                try:
                    ingest.process_drive_upload(db, file_meta, folder_id, source_kind, fixed_entity)
                except Exception:
                    # A single bad file must never break the scan of the rest.
                    logger.exception("Upload scan: processing file %s failed", file_meta.get("id"))
    except Exception:
        logger.exception("Upload scan encountered an unexpected error")
    finally:
        db.close()


def _retry_failed_uploads_once() -> None:
    """Retry drive-upload rows that failed or are parked on the API limit (the Gmail retry loop
    excludes them by design)."""
    settings = get_settings()
    if not (settings.enable_real_google and settings.uploads_scan_enabled) or settings.process_new_only:
        return

    from app.db.models import ProcessedEmail
    from app.services.upload_ingest_service import UploadIngestService

    db = SessionLocal()
    try:
        now = utc_now()
        failed_cutoff = now - timedelta(minutes=5)
        api_limit_cutoff = now - _API_LIMIT_RETRY_AFTER
        rows = db.execute(
            select(ProcessedEmail)
            .where(
                ProcessedEmail.gmail_message_id.like("drive-upload:%"),
                (
                    # 'failed' = a problem with this specific file; give up after N attempts.
                    (ProcessedEmail.status == "failed")
                    & (ProcessedEmail.updated_at < failed_cutoff)
                    & (ProcessedEmail.attempts < _MAX_AUTO_RETRY_ATTEMPTS)
                ) | (
                    # 'waiting_api_limit' = account-level block (daily cap or billing) -- no
                    # ceiling, see the matching comment in _retry_failed_emails_once above.
                    (ProcessedEmail.status == "waiting_api_limit") & (ProcessedEmail.updated_at < api_limit_cutoff)
                ),
            )
            .order_by(ProcessedEmail.updated_at.asc())
            .limit(5)
        ).scalars().all()
        if not rows:
            return
        ingest = UploadIngestService()
        for row in rows:
            upload = (row.metadata_json or {}).get("upload") or {}
            file_id = upload.get("drive_file_id")
            source_folder_id = upload.get("source_folder_id")
            if not file_id or not source_folder_id:
                continue
            file_meta = {
                "id": file_id,
                "name": upload.get("original_filename"),
                "mimeType": upload.get("mime_type"),
            }
            try:
                ingest.process_drive_upload(db, file_meta, source_folder_id, upload.get("source_kind"), upload.get("fixed_entity"))
            except Exception:
                logger.exception("Upload retry failed for %s", row.gmail_message_id)
    except Exception:
        logger.exception("Upload retry loop encountered an unexpected error")
    finally:
        db.close()


async def _uploads_scan_loop() -> None:
    interval = max(60, get_settings().uploads_scan_interval_minutes * 60)
    await asyncio.sleep(interval)  # let startup settle before the first scan
    while True:
        await asyncio.to_thread(_scan_uploads_once)
        await asyncio.to_thread(_retry_failed_uploads_once)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    tasks: list[asyncio.Task] = []

    if settings.gmail_watch_renew_interval_hours > 0:
        interval = settings.gmail_watch_renew_interval_hours * 3600
        tasks.append(asyncio.create_task(_watch_renewal_loop(interval)))

    if settings.enable_real_google:
        tasks.append(asyncio.create_task(_retry_loop()))
        tasks.append(asyncio.create_task(_safety_net_loop()))
        if settings.uploads_scan_enabled:
            tasks.append(asyncio.create_task(_uploads_scan_loop()))

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    init_db()
    RulebookService().reload_from_db_or_file()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Public routers: health, auth (login + Google OAuth callback), and the Pub/Sub
    # webhook (called by Google, secured separately).
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(webhooks.router)

    # Dashboard API: require a valid bearer token (issued by POST /auth/login).
    protected = [Depends(get_current_user)]
    app.include_router(emails.router, dependencies=protected)
    app.include_router(activity.router, dependencies=protected)
    app.include_router(review.router, dependencies=protected)
    app.include_router(documents.router, dependencies=protected)
    app.include_router(entities.router, dependencies=protected)
    app.include_router(notifications.router, dependencies=protected)
    app.include_router(admin.router, dependencies=protected)
    app.include_router(google.router, dependencies=protected)

    # Admin-only: manage additional dashboard logins.
    app.include_router(users.router, dependencies=[Depends(require_admin)])
    return app


app = create_app()

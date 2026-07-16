from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import GmailWatchState
from app.services.gmail_service import GmailService


class WatchTopicNotConfigured(ValueError):
    """Raised when no Pub/Sub topic is available to start/renew a Gmail watch."""


def _millis_to_datetime(value: str | int | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def renew_watch(
    db: Session,
    *,
    topic_name: str | None = None,
    label_ids: list[str] | None = None,
    label_filter_behavior: str | None = None,
) -> GmailWatchState:
    # Start or renew the Gmail watch and upsert the persisted watch state. Gmail watches
    # expire after ~7 days, so this is called both by the admin route and the in-app
    # daily renewal loop. Re-watching is idempotent: it resets the expiration and returns
    # the current historyId, which becomes the cursor for the next Pub/Sub notification.
    settings = get_settings()
    topic_name = topic_name or settings.gmail_pubsub_topic_name
    if not topic_name:
        raise WatchTopicNotConfigured(
            "topic_name is required. Provide it explicitly or set GMAIL_PUBSUB_TOPIC_NAME."
        )
    label_ids = label_ids if label_ids is not None else settings.gmail_pubsub_label_ids
    label_filter_behavior = label_filter_behavior or settings.gmail_pubsub_label_filter_behavior

    gmail = GmailService()
    email_address = gmail.get_profile().get("emailAddress")
    response = gmail.start_watch(
        topic_name=topic_name,
        label_ids=label_ids,
        label_filter_behavior=label_filter_behavior,
    )

    state = None
    if email_address:
        state = db.execute(
            select(GmailWatchState).where(GmailWatchState.email_address == email_address)
        ).scalars().first()
    if not state:
        state = db.execute(
            select(GmailWatchState).order_by(GmailWatchState.updated_at.desc())
        ).scalars().first()
    if not state:
        state = GmailWatchState()

    state.email_address = email_address
    state.topic_name = topic_name
    state.label_ids = label_ids or []
    state.label_filter_behavior = label_filter_behavior
    state.history_id = str(response.get("historyId") or "")
    state.expiration_at = _millis_to_datetime(response.get("expiration"))
    state.active = True
    state.last_error = None
    state.metadata_json = {"watch_response": response}
    db.add(state)
    db.commit()
    db.refresh(state)
    return state

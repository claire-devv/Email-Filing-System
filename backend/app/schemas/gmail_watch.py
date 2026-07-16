from pydantic import BaseModel, Field

from app.schemas.common import UtcDateTime


class GmailWatchStartRequest(BaseModel):
    topic_name: str | None = None
    label_ids: list[str] | None = None
    label_filter_behavior: str | None = Field(default=None, pattern="^(INCLUDE|EXCLUDE)$")


class GmailWatchStatusResponse(BaseModel):
    active: bool
    email_address: str | None = None
    topic_name: str | None = None
    label_ids: list[str] = []
    label_filter_behavior: str | None = None
    history_id: str | None = None
    expiration_at: UtcDateTime | None = None
    last_notification_at: UtcDateTime | None = None
    last_successful_sync_at: UtcDateTime | None = None
    last_error: str | None = None


class GmailWatchStartResponse(GmailWatchStatusResponse):
    message: str

from pydantic import BaseModel


class ProcessEmailResponse(BaseModel):
    gmail_message_id: str
    status: str
    email_id: int | None = None
    review_id: int | None = None
    message: str


class ProcessUnreadRequest(BaseModel):
    limit: int = 20
    newer_than_minutes: int | None = None


class ProcessUnreadResponse(BaseModel):
    processed_count: int
    skipped_count: int
    waiting_count: int = 0
    results: list[ProcessEmailResponse]

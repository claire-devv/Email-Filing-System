from typing import Any

from pydantic import BaseModel

from app.schemas.common import ArtifactOut, ORMModel, UtcDateTime


class ReviewEmailOut(ORMModel):
    sender: str | None = None
    subject: str | None = None
    received_at: UtcDateTime | None = None
    gmail_message_id: str | None = None


class ReviewItemOut(ORMModel):
    id: int
    email_id: int
    status: str
    # Where this item came from: "email" (default), "client_uploads", or "rres_uploads".
    source: str = "email"
    email: ReviewEmailOut | None = None
    proposed: dict[str, Any] = {}
    corrected: dict[str, Any] = {}
    final: dict[str, Any] = {}
    decision_audit: dict[str, Any] = {}
    urgent: bool
    reviewer_decision: str | None = None
    reviewed_by: str | None = None
    reviewed_at: UtcDateTime | None = None
    created_at: UtcDateTime
    # First lines of the email body, so the list view can show what the email says.
    body_preview: str | None = None
    artifacts: list[ArtifactOut] = []


class ReviewApproveRequest(BaseModel):
    reviewed_by: str | None = None


class ReviewCorrectRequest(BaseModel):
    entity: str
    level2: str
    level3: str | None = None
    file_summary: str
    document_date: str | None = None
    reviewed_by: str | None = None
    alias: str | None = None
    notes: str | None = None
    # Reviewer opt-out: when false the correction files normally but is not stored
    # as a learned mapping for future classifications.
    learn: bool = True


class ReviewRejectRequest(BaseModel):
    reason: str | None = None
    reviewed_by: str | None = None


class SplitAssignment(BaseModel):
    # One attachment routed to its own entity/folder in a multi-entity split.
    artifact_id: int
    entity: str
    level2: str
    level3: str | None = None
    file_summary: str


class ReviewSplitRequest(BaseModel):
    # Per-attachment routing for a multi-entity email: each real attachment is filed to its own
    # entity, and the combined email PDF is copied into every involved entity's Communications.
    assignments: list[SplitAssignment]
    # One document date for the whole email (applied to every attachment + the combined PDF),
    # mirroring the single-entity Correct flow.
    document_date: str | None = None
    reviewed_by: str | None = None
    # Accepted for forward-compatibility; v1 does not record learned mappings for a split
    # (email-level signals cannot be attributed to one of several entities).
    learn: bool = False

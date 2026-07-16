from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ProcessedEmail(Base, TimestampMixin):
    __tablename__ = "processed_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_message_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(128))
    sender: Mapped[str | None] = mapped_column(String(512))
    subject: Mapped[str | None] = mapped_column(String(1000))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(64), default="new", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    artifacts: Mapped[list["FileArtifact"]] = relationship(back_populates="email", cascade="all, delete-orphan")
    needs_review_items: Mapped[list["NeedsReview"]] = relationship(back_populates="email")
    filing_logs: Mapped[list["FilingLog"]] = relationship(back_populates="email")


class FileArtifact(Base, TimestampMixin):
    __tablename__ = "file_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("processed_emails.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    local_path: Mapped[str] = mapped_column(String(2000), nullable=False)
    generated_pdf_path: Mapped[str | None] = mapped_column(String(2000))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    file_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    drive_file_id: Mapped[str | None] = mapped_column(String(255))
    drive_link: Mapped[str | None] = mapped_column(String(2000))
    drive_folder_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64), default="prepared", index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    email: Mapped[ProcessedEmail] = relationship(back_populates="artifacts")


class ProcessedFile(Base, TimestampMixin):
    __tablename__ = "processed_files"
    __table_args__ = (UniqueConstraint("file_hash", "drive_folder_id", name="uq_processed_file_hash_folder"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    drive_folder_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    drive_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    source_email_id: Mapped[int | None] = mapped_column(ForeignKey("processed_emails.id"))


class FilingLog(Base, TimestampMixin):
    __tablename__ = "filing_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int | None] = mapped_column(ForeignKey("processed_emails.id"), index=True)
    sender: Mapped[str | None] = mapped_column(String(512))
    subject: Mapped[str | None] = mapped_column(String(1000))
    entity: Mapped[str | None] = mapped_column(String(512))
    folder_path: Mapped[str | None] = mapped_column(String(2000))
    confidence: Mapped[float | None] = mapped_column(Float)
    drive_link: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str | None] = mapped_column(Text)
    processing_time_ms: Mapped[int | None] = mapped_column(Integer)

    email: Mapped[ProcessedEmail | None] = relationship(back_populates="filing_logs")


class NeedsReview(Base, TimestampMixin):
    __tablename__ = "needs_review"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("processed_emails.id"), index=True, nullable=False)
    proposed_entity: Mapped[str | None] = mapped_column(String(512))
    proposed_level2: Mapped[str | None] = mapped_column(String(255))
    proposed_level3: Mapped[str | None] = mapped_column(String(255))
    proposed_file_summary: Mapped[str | None] = mapped_column(String(1000))
    claude_reasoning: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    urgent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="pending", index=True)
    reviewer_decision: Mapped[str | None] = mapped_column(String(64))
    corrected_entity: Mapped[str | None] = mapped_column(String(512))
    corrected_level2: Mapped[str | None] = mapped_column(String(255))
    corrected_level3: Mapped[str | None] = mapped_column(String(255))
    corrected_file_summary: Mapped[str | None] = mapped_column(String(1000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    email: Mapped[ProcessedEmail] = relationship(back_populates="needs_review_items")


class LearnedMapping(Base, TimestampMixin):
    __tablename__ = "learned_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pattern_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    pattern_value: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    entity: Mapped[str | None] = mapped_column(String(512))
    level2: Mapped[str | None] = mapped_column(String(255))
    level3: Mapped[str | None] = mapped_column(String(255))
    confidence_boost: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    confirmation_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source: Mapped[str | None] = mapped_column(String(128))
    # Reviewer's free-text rationale captured on Correct ("why this goes here"); surfaced
    # back to the classifier so future similar emails learn the reasoning, not just the folder.
    note: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Entity(Base, TimestampMixin):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_name: Mapped[str] = mapped_column(String(512), unique=True, index=True, nullable=False)
    folder_name: Mapped[str] = mapped_column(String(512), nullable=False)
    drive_folder_id: Mapped[str | None] = mapped_column(String(255))
    aliases: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    properties: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class FolderRulebook(Base, TimestampMixin):
    __tablename__ = "folder_rulebooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    source_path: Mapped[str] = mapped_column(String(2000), nullable=False)
    rules_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ApiUsage(Base, TimestampMixin):
    __tablename__ = "api_usage"
    __table_args__ = (UniqueConstraint("provider", "usage_date", name="uq_api_usage_provider_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class GmailWatchState(Base, TimestampMixin):
    __tablename__ = "gmail_watch_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_address: Mapped[str | None] = mapped_column(String(512), unique=True, index=True)
    topic_name: Mapped[str | None] = mapped_column(String(1000))
    label_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    label_filter_behavior: Mapped[str | None] = mapped_column(String(64))
    history_id: Mapped[str | None] = mapped_column(String(128), index=True)
    expiration_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_notification_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class DashboardUser(Base, TimestampMixin):
    __tablename__ = "dashboard_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

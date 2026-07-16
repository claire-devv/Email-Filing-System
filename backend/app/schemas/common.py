from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer


def _to_utc_z(value: datetime) -> str:
    # SQLite drops tzinfo, so datetimes read back from the DB are naive UTC. Tag them as UTC
    # and emit an explicit "Z" so the browser parses an unambiguous instant instead of
    # guessing local time (the cause of the dashboard's wrong / 1-hour-off timestamps).
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# Use in place of `datetime` on every API response field so all timestamps go out as UTC.
UtcDateTime = Annotated[datetime, PlainSerializer(_to_utc_z, return_type=str, when_used="json")]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ApiMessage(BaseModel):
    message: str
    data: dict[str, Any] | None = None


class ArtifactOut(ORMModel):
    id: int
    kind: str
    original_filename: str | None = None
    local_path: str
    generated_pdf_path: str | None = None
    mime_type: str | None = None
    file_hash: str | None = None
    size_bytes: int | None = None
    drive_file_id: str | None = None
    drive_link: str | None = None
    drive_folder_id: str | None = None
    status: str
    # Name the file will have (or has) in Drive; filled where the caller knows it.
    drive_filename: str | None = None
    # Agreed-decorative signature/logo image (part-classifier + Claude both said so). The split
    # UI hides these from assignment rows -- they are never filed standalone.
    decorative: bool = False


class ActivityItem(ORMModel):
    id: int
    created_at: UtcDateTime
    received_at: UtcDateTime | None = None
    processing_time_ms: int | None = None
    sender: str | None = None
    subject: str | None = None
    entity: str | None = None
    folder_path: str | None = None
    # Drive folder id for THIS row's destination. Distinct per entity on a multi-entity split,
    # so each activity row's "Open folder" link points to the right client folder.
    folder_drive_id: str | None = None
    confidence: float | None = None
    drive_link: str | None = None
    status: str
    message: str | None = None
    # Where this item came from: "email" (default), "client_uploads", or "rres_uploads".
    source: str = "email"
    decision_audit: dict[str, Any] = {}
    processing_metadata: dict[str, Any] = {}
    artifacts: list[ArtifactOut] = []

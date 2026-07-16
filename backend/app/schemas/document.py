from pydantic import BaseModel

from app.schemas.common import ORMModel, UtcDateTime


class DocumentOut(ORMModel):
    id: int
    filed_at: UtcDateTime
    filename: str
    original_filename: str | None = None
    kind: str
    status: str
    entity: str | None = None
    folder_path: str | None = None
    drive_file_id: str
    drive_link: str | None = None
    drive_folder_id: str | None = None
    size_bytes: int | None = None
    sender: str | None = None
    subject: str | None = None
    received_at: UtcDateTime | None = None
    # Where the document came from: "email" (default), "client_uploads", or "rres_uploads".
    source: str = "email"


class DocumentListOut(BaseModel):
    items: list[DocumentOut]
    total: int

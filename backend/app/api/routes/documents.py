import mimetypes
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from googleapiclient.errors import HttpError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import FileArtifact, FilingLog, ProcessedEmail, ProcessedFile
from app.db.session import get_db
from app.schemas.document import DocumentListOut, DocumentOut
from app.services.drive_service import DriveService

router = APIRouter(prefix="/documents", tags=["documents"])

FILED_STATUSES = ("filed", "duplicate")


def _parse_date(value: str | None, field: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field} date: {value!r} (expected YYYY-MM-DD).") from exc


@router.get("", response_model=DocumentListOut)
def list_documents(
    q: str = "",
    entity: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> DocumentListOut:
    # Filed documents straight from our own records — no live Drive listing. The
    # latest FilingLog per email carries the final entity/path after corrections.
    latest_log = (
        select(FilingLog.email_id, func.max(FilingLog.id).label("log_id"))
        .where(FilingLog.email_id.is_not(None))
        .group_by(FilingLog.email_id)
    ).subquery()

    stmt = (
        select(FileArtifact, ProcessedEmail, FilingLog, ProcessedFile.filename)
        .join(ProcessedEmail, FileArtifact.email_id == ProcessedEmail.id)
        .outerjoin(latest_log, latest_log.c.email_id == FileArtifact.email_id)
        .outerjoin(FilingLog, FilingLog.id == latest_log.c.log_id)
        .outerjoin(ProcessedFile, ProcessedFile.drive_file_id == FileArtifact.drive_file_id)
        .where(FileArtifact.status.in_(FILED_STATUSES), FileArtifact.drive_file_id.is_not(None))
    )

    if q.strip():
        needle = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                ProcessedFile.filename.ilike(needle),
                FileArtifact.original_filename.ilike(needle),
                ProcessedEmail.subject.ilike(needle),
                ProcessedEmail.sender.ilike(needle),
                FilingLog.entity.ilike(needle),
            )
        )
    if entity:
        stmt = stmt.where(FilingLog.entity == entity)
    if kind:
        stmt = stmt.where(FileArtifact.kind == kind)
    if from_dt := _parse_date(date_from, "date_from"):
        stmt = stmt.where(FileArtifact.updated_at >= from_dt)
    if to_dt := _parse_date(date_to, "date_to"):
        stmt = stmt.where(FileArtifact.updated_at <= to_dt.replace(hour=23, minute=59, second=59))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(FileArtifact.updated_at.desc())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    ).all()

    items = [
        DocumentOut(
            id=artifact.id,
            filed_at=artifact.updated_at,
            filename=filed_name or artifact.original_filename or artifact.kind,
            original_filename=artifact.original_filename,
            kind=artifact.kind,
            status=artifact.status,
            entity=log.entity if log else None,
            folder_path=(artifact.metadata_json or {}).get("folder_path") or (log.folder_path if log else None),
            drive_file_id=artifact.drive_file_id,
            drive_link=artifact.drive_link,
            drive_folder_id=artifact.drive_folder_id,
            size_bytes=artifact.size_bytes,
            sender=email.sender,
            subject=email.subject,
            received_at=email.received_at,
            source=((email.metadata_json or {}).get("upload") or {}).get("source_kind", "email"),
        )
        for artifact, email, log, filed_name in rows
    ]
    return DocumentListOut(items=items, total=total)


@router.get("/{artifact_id}/download")
def download_document(artifact_id: int, db: Session = Depends(get_db)) -> Response:
    artifact = db.get(FileArtifact, artifact_id)
    if not artifact or not artifact.drive_file_id or artifact.status not in FILED_STATUSES:
        raise HTTPException(status_code=404, detail="Document not found.")

    drive = DriveService()
    try:
        item = drive.get_drive_item(artifact.drive_file_id)
    except RuntimeError as exc:  # Google account not connected
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HttpError as exc:
        raise HTTPException(status_code=502, detail=f"Google Drive error: {exc.status_code}") from exc
    if not item or item.get("trashed"):
        raise HTTPException(status_code=404, detail="File is no longer available in Google Drive.")

    filed_name = db.execute(
        select(ProcessedFile.filename).where(ProcessedFile.drive_file_id == artifact.drive_file_id)
    ).scalars().first()
    filename = filed_name or item.get("name") or artifact.original_filename or f"document-{artifact.id}"
    # The download was previously always advertised as application/pdf regardless of the
    # actual file (e.g. .xlsx spreadsheets filed alongside PDFs) -- the browser's built-in PDF
    # viewer would then try to parse non-PDF bytes and fail with "Failed to load PDF document".
    # Drive's own mimeType is the most authoritative source; fall back to our stored mime_type,
    # then guess from the filename extension.
    media_type = (
        item.get("mimeType")
        or artifact.mime_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    # Buffer the whole file instead of streaming it. A chunked/close-delimited StreamingResponse
    # body (no Content-Length) was completing cleanly on the backend and at nginx -- both logged
    # 200 OK with no errors -- but large downloads still failed with net::ERR_FAILED in the
    # browser, pointing at ambiguous end-of-body framing across the HTTP/1.0 backend connection,
    # nginx, and an HTTP/2 client. A plain Response with an exact Content-Length (computed from
    # the bytes actually downloaded, not Drive's possibly-stale size metadata) removes that
    # ambiguity entirely. Filed documents are bounded in size, so buffering is safe.
    data = b"".join(drive.download_file_stream(artifact.drive_file_id))
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return Response(content=data, media_type=media_type, headers=headers)

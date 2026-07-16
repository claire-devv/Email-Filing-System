import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Entity, FileArtifact, FilingLog, LearnedMapping, NeedsReview, ProcessedEmail
from app.db.session import get_db
from app.schemas.common import ArtifactOut
from app.schemas.review import (
    ReviewApproveRequest,
    ReviewCorrectRequest,
    ReviewItemOut,
    ReviewRejectRequest,
    ReviewSplitRequest,
)
from app.services.filing_service import FilingService
from app.services.review_service import ReviewService
from app.services.types import ClassificationResult
from app.utils.files import resolve_artifact_path

router = APIRouter(prefix="/review/items", tags=["review"])


@router.get("", response_model=list[ReviewItemOut])
def list_review_items(
    status: str = "pending",
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[ReviewItemOut]:
    # Newest email first, on a single timeline. Use the email's received time when present,
    # else fall back to the row's processing time -- via COALESCE so an email with a NULL
    # received_at (forwarded/edge cases) is ranked by created_at instead of sinking to the
    # bottom (NULLs sort last under a plain DESC). id.desc() is the stable tiebreaker for
    # emails that share a timestamp (e.g. a batch sent in the same second). Urgency is shown
    # with a row dot, not by reordering.
    received_or_created = func.coalesce(ProcessedEmail.received_at, NeedsReview.created_at)
    items = db.execute(
        select(NeedsReview)
        .where(NeedsReview.status == status)
        .outerjoin(NeedsReview.email)
        .options(selectinload(NeedsReview.email))
        .order_by(received_or_created.desc(), NeedsReview.id.desc())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    ).scalars().all()
    return [_review_out(db, item) for item in items]


@router.get("/level3-options")
def level3_options(level2: str, entity: str | None = None, db: Session = Depends(get_db)) -> dict:
    # Known Level 3 values previously learned for this subfolder (e.g. bank names under
    # Bank Statements), offered as suggestions — free text is still allowed.
    stmt = (
        select(LearnedMapping.level3)
        .where(
            LearnedMapping.active.is_(True),
            LearnedMapping.level2 == level2,
            LearnedMapping.level3.is_not(None),
        )
        .distinct()
    )
    if entity:
        stmt = stmt.where(LearnedMapping.entity == entity)
    options = sorted({value for value in db.execute(stmt).scalars().all() if value and value.strip()})
    return {"options": options}


@router.get("/{review_id}/artifacts/{artifact_id}/file")
def artifact_file(review_id: int, artifact_id: int, db: Session = Depends(get_db)) -> FileResponse:
    item = db.get(NeedsReview, review_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found.")
    artifact = db.get(FileArtifact, artifact_id)
    if not artifact or artifact.email_id != item.email_id:
        raise HTTPException(status_code=404, detail="Artifact not found for this review item.")
    path = resolve_artifact_path(artifact.generated_pdf_path) or resolve_artifact_path(artifact.local_path)
    if not path:
        raise HTTPException(status_code=404, detail="Artifact file is not available on the server.")
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else (artifact.mime_type or "application/octet-stream")
    filename = artifact.original_filename or path.name
    return FileResponse(path, media_type=media_type, filename=filename, content_disposition_type="inline")


@router.post("/{review_id}/approve", response_model=ReviewItemOut)
def approve(review_id: int, payload: ReviewApproveRequest, db: Session = Depends(get_db)) -> ReviewItemOut:
    item = _get_pending_review(db, review_id)
    try:
        updated = ReviewService().approve(db, item, payload.reviewed_by)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _review_out(db, updated)


@router.post("/{review_id}/correct", response_model=ReviewItemOut)
def correct(review_id: int, payload: ReviewCorrectRequest, db: Session = Depends(get_db)) -> ReviewItemOut:
    item = _get_pending_review(db, review_id)
    try:
        updated = ReviewService().correct(
            db,
            item,
            payload.entity,
            payload.level2,
            payload.level3,
            payload.file_summary,
            payload.document_date,
            payload.reviewed_by,
            payload.alias,
            payload.notes,
            payload.learn,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _review_out(db, updated)


@router.post("/{review_id}/file-split", response_model=ReviewItemOut)
def file_split(review_id: int, payload: ReviewSplitRequest, db: Session = Depends(get_db)) -> ReviewItemOut:
    item = _get_pending_review(db, review_id)
    try:
        updated = ReviewService().file_split(
            db,
            item,
            payload.assignments,
            payload.document_date,
            payload.reviewed_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _review_out(db, updated)


@router.post("/{review_id}/reject", response_model=ReviewItemOut)
def reject(review_id: int, payload: ReviewRejectRequest, db: Session = Depends(get_db)) -> ReviewItemOut:
    item = _get_pending_review(db, review_id)
    updated = ReviewService().reject(db, item, payload.reason, payload.reviewed_by)
    return _review_out(db, updated)


def _get_pending_review(db: Session, review_id: int) -> NeedsReview:
    item = db.get(NeedsReview, review_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found.")
    if item.status != "pending":
        raise HTTPException(status_code=409, detail=f"Review item is already {item.status}.")
    return item


def _review_out(db: Session, item: NeedsReview) -> ReviewItemOut:
    artifacts = db.execute(select(FileArtifact).where(FileArtifact.email_id == item.email_id)).scalars().all()
    body_artifact = next((a for a in artifacts if a.kind == "email_body"), None)
    _raw_preview = ((body_artifact.metadata_json or {}).get("text_preview") or "") if body_artifact else ""
    # Strip forwarded-message header boilerplate ("---------- Forwarded message ---------",
    # "From: ...", "Date: ...", "Subject: ...", "To: ...") from the top so the snippet shows
    # the actual message body rather than the forwarding wrapper.
    _preview_lines = _raw_preview.splitlines()
    _skip = re.compile(r"^(-{3,}.*-{3,}|From\s*:|Date\s*:|Subject\s*:|To\s*:|Cc\s*:|Fwd\s*:)", re.I)
    while _preview_lines and (not _preview_lines[0].strip() or _skip.match(_preview_lines[0].strip())):
        _preview_lines.pop(0)
    body_preview = (" ".join(_preview_lines).strip()[:280] or None) or None
    latest_log = db.execute(
        select(FilingLog).where(FilingLog.email_id == item.email_id).order_by(FilingLog.created_at.desc())
    ).scalars().first()
    metadata = item.metadata_json or {}
    final = metadata.get("final") or {
        "entity": item.corrected_entity,
        "level2": item.corrected_level2,
        "level3": item.corrected_level3,
        "file_summary": item.corrected_file_summary,
        "document_date": None,
        "folder_path": latest_log.folder_path if latest_log else None,
    }
    final.setdefault("folder_path", latest_log.folder_path if latest_log else None)
    is_known_entity = bool(
        db.scalar(select(Entity.id).where(Entity.entity_name == item.proposed_entity, Entity.active.is_(True)))
    ) if item.proposed_entity else False
    # For multi-entity split emails, expose which of the proposed split entities are already
    # known in the registry so the frontend can show per-row known/new badges without extra calls.
    # Collect all entity names from both auto_split_entities and per-attachment classifications
    # so this works even when auto_split_entities was not written (older items / pre-fix emails).
    decision_audit = metadata.get("decision_audit") or {}
    candidate_entities: set[str] = set(decision_audit.get("auto_split_entities") or [])
    for v in (decision_audit.get("artifact_classifications") or {}).values():
        if isinstance(v, dict) and v.get("entity"):
            candidate_entities.add(v["entity"])
    if item.proposed_entity:
        candidate_entities.add(item.proposed_entity)
    known_entity_names: set[str] = set()
    if candidate_entities:
        rows = db.execute(
            select(Entity.entity_name).where(Entity.entity_name.in_(candidate_entities), Entity.active.is_(True))
        ).scalars().all()
        known_entity_names = set(rows)
    # Names the files will carry in Drive, from the same logic filing uses. Corrected
    # values win over proposed so the chips track the reviewer's decision.
    classification = ClassificationResult(
        entity=item.corrected_entity or item.proposed_entity,
        level2=item.corrected_level2 or item.proposed_level2,
        level3=item.corrected_level3 or item.proposed_level3,
        file_summary=item.corrected_file_summary or item.proposed_file_summary or "Filing Document",
        confidence=item.confidence or 0,
        unknown_entity=False,
        needs_review=False,
        reason="",
        document_date=metadata.get("corrected_document_date") or metadata.get("proposed_document_date"),
        decision_audit=metadata.get("decision_audit") or {},
    )
    try:
        drive_names = FilingService().drive_filenames(item.email, classification, artifacts)
    except Exception:  # never let a naming edge case break the review list
        drive_names = {}
    artifacts_out = []
    for a in artifacts:
        out = ArtifactOut.model_validate(a)
        out.drive_filename = drive_names.get(a.id)
        out.decorative = bool((a.metadata_json or {}).get("decorative"))
        artifacts_out.append(out)
    return ReviewItemOut.model_validate(
        {
            "id": item.id,
            "email_id": item.email_id,
            "status": item.status,
            "source": ((item.email.metadata_json or {}).get("upload") or {}).get("source_kind", "email") if item.email else "email",
            "email": item.email,
            "proposed": {
                "entity": item.proposed_entity,
                "level2": item.proposed_level2,
                "level3": item.proposed_level3,
                "file_summary": item.proposed_file_summary,
                "document_date": metadata.get("proposed_document_date"),
                "confidence": item.confidence,
                "reason": item.claude_reasoning,
                "is_known_entity": is_known_entity,
                "known_split_entities": sorted(known_entity_names),
            },
            "corrected": {
                "entity": item.corrected_entity,
                "level2": item.corrected_level2,
                "level3": item.corrected_level3,
                "file_summary": item.corrected_file_summary,
                "document_date": metadata.get("corrected_document_date"),
            },
            "final": final,
            "decision_audit": metadata.get("decision_audit") or {},
            "urgent": item.urgent,
            "reviewer_decision": item.reviewer_decision,
            "reviewed_by": item.reviewed_by,
            "reviewed_at": item.reviewed_at,
            "created_at": item.created_at,
            "body_preview": body_preview,
            "artifacts": artifacts_out,
        }
    )

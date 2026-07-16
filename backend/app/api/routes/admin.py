from fastapi import APIRouter, Depends, Header, HTTPException
from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import ApiUsage, FileArtifact, FilingLog, GmailWatchState, NeedsReview, ProcessedEmail, ProcessedFile
from app.db.session import get_db
from app.schemas.common import _to_utc_z
from app.schemas.gmail_watch import GmailWatchStartRequest, GmailWatchStartResponse, GmailWatchStatusResponse
from app.schemas.rulebook import FolderRulebookOut, FolderRulebookUpdate
from app.services.decision_service import DecisionValidator
from app.services.drive_service import DriveService
from app.services.entity_service import EntityService
from app.services.filing_service import FilingService
from app.services.gmail_service import GmailService
from app.services.processing_service import MAX_AUTO_RETRY_ATTEMPTS
from app.services.rulebook_service import RulebookService
from app.services.upload_ingest_service import UPLOAD_ID_PREFIX
from app.services import watch_service
from app.services.watch_service import WatchTopicNotConfigured

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/folder-rules", response_model=FolderRulebookOut)
def get_folder_rules() -> dict:
    rules = RulebookService().get()
    return {"version": rules["version"], "rules": rules}


@router.post("/folder-rules/reload", response_model=FolderRulebookOut)
def reload_folder_rules() -> dict:
    rules = RulebookService().reload_from_file_safely()
    return {"version": rules["version"], "rules": rules}


@router.put("/folder-rules", response_model=FolderRulebookOut)
def update_folder_rules(payload: FolderRulebookUpdate) -> dict:
    rules = RulebookService().update_file_and_cache(payload.rules)
    return {"version": rules["version"], "rules": rules}


@router.get("/api-usage")
def api_usage(db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(ApiUsage).order_by(ApiUsage.usage_date.desc(), ApiUsage.provider)).scalars().all()
    return {
        "usage": [
            {
                "provider": row.provider,
                "date": row.usage_date,
                "call_count": row.call_count,
            }
            for row in rows
        ]
    }


@router.get("/failed-emails")
def failed_emails(db: Session = Depends(get_db)) -> dict:
    """
    One row per email/upload currently sitting in 'failed' or 'waiting_api_limit' -- NOT one row
    per retry attempt (that's what the Activity feed shows, via FilingLog, which logs an entry
    per attempt). Distinguishes:
      - stuck: a 'failed' row that has exhausted the retry ceiling (or is flagged
        permanent_failure, e.g. a password-protected file) -- the automatic retry loop has given
        up; a human needs to look at it.
      - retrying: still within the retry ceiling ('failed'), or 'waiting_api_limit' (an
        account/API-level block -- rate limit, Anthropic server error, network issue, expired key,
        or billing -- which retries indefinitely with no ceiling; see app/utils/errors.py).
    source distinguishes a Gmail email from a Drive "Client Uploads" / "RRES Uploads" file (see
    DRIVE_UPLOADS_PLAN.md) -- both pipelines share this same ProcessedEmail table and status model.
    """
    rows = db.execute(
        select(ProcessedEmail)
        .where(ProcessedEmail.status.in_(["failed", "waiting_api_limit"]))
        .order_by(ProcessedEmail.updated_at.desc())
    ).scalars().all()

    items = []
    stuck_count = 0
    for row in rows:
        meta = row.metadata_json or {}
        upload = meta.get("upload") or {}
        is_upload = row.gmail_message_id.startswith(UPLOAD_ID_PREFIX)
        source = upload.get("source_kind") if is_upload else "email"  # "client_uploads" | "rres_uploads" | "email"
        permanent = bool(meta.get("permanent_failure"))
        stuck = row.status == "failed" and (permanent or row.attempts >= MAX_AUTO_RETRY_ATTEMPTS)
        if stuck:
            stuck_count += 1
        items.append(
            {
                "id": row.id,
                "gmail_message_id": row.gmail_message_id,
                "source": source,
                "subject": row.subject,
                "sender": row.sender,
                "status": row.status,
                "attempts": row.attempts,
                "stuck": stuck,
                "permanent_failure": permanent,
                "retryable_reason": meta.get("retryable_reason"),
                "last_error": row.last_error,
                "received_at": _to_utc_z(row.received_at) if row.received_at else None,
                "updated_at": _to_utc_z(row.updated_at) if row.updated_at else None,
            }
        )

    return {
        "total": len(items),
        "stuck": stuck_count,
        "retrying": len(items) - stuck_count,
        "items": items,
    }


@router.post("/repair/review/{review_id}")
def repair_review_item(
    review_id: int,
    db: Session = Depends(get_db),
    x_rres_admin_key: str | None = Header(default=None, alias="X-RRES-Admin-Key"),
) -> dict:
    _require_admin_key(x_rres_admin_key)
    item = db.get(NeedsReview, review_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found.")

    metadata = item.metadata_json or {}
    final = metadata.get("final") or {}
    validator = DecisionValidator()
    decision = validator.from_review_values(
        entity=item.corrected_entity or final.get("entity") or item.proposed_entity,
        level2=item.corrected_level2 or final.get("level2") or item.proposed_level2,
        level3=item.corrected_level3 or final.get("level3") or item.proposed_level3,
        file_summary=item.corrected_file_summary or final.get("file_summary") or item.proposed_file_summary,
        document_date=metadata.get("corrected_document_date") or final.get("document_date") or metadata.get("proposed_document_date"),
        reason="Admin repair.",
        confidence=100,
    )
    validation = validator.validate(
        decision,
        item.email,
        [],
        EntityService().list_active(db),
        allow_new_entity=True,
        force_file=True,
    )
    if not validation.should_file:
        raise HTTPException(status_code=422, detail=f"Repair decision is invalid: {', '.join(validation.reasons)}")

    artifacts = db.execute(select(FileArtifact).where(FileArtifact.email_id == item.email_id)).scalars().all()
    before = [
        {
            "artifact_id": artifact.id,
            "drive_file_id": artifact.drive_file_id,
            "drive_folder_id": artifact.drive_folder_id,
            "filename": _processed_filename(db, artifact),
            "status": artifact.status,
        }
        for artifact in artifacts
    ]
    FilingService().file_email_artifacts(db, item.email, validation.decision, artifacts)
    target_path = f"{get_settings().drive_root_name} / {validation.decision.entity} / {validation.decision.level2}"
    if validation.decision.level3:
        target_path += f" / {validation.decision.level3}"
    item.metadata_json = {
        **metadata,
        "repair_audit": {
            "before": before,
            "after": [
                {
                    "artifact_id": artifact.id,
                    "drive_file_id": artifact.drive_file_id,
                    "drive_folder_id": artifact.drive_folder_id,
                    "filename": _processed_filename(db, artifact),
                    "status": artifact.status,
                }
                for artifact in artifacts
            ],
            "decision_audit": validation.audit,
        },
        "final": {
            "entity": validation.decision.entity,
            "level2": validation.decision.level2,
            "level3": validation.decision.level3,
            "file_summary": validation.decision.file_summary,
            "document_date": validation.decision.document_date,
        },
    }
    db.add(
        FilingLog(
            email_id=item.email_id,
            sender=item.email.sender,
            subject=item.email.subject,
            entity=validation.decision.entity,
            folder_path=target_path,
            confidence=validation.decision.confidence,
            status="repair",
            message="Admin repaired Drive folder/name using final decision.",
        )
    )
    db.commit()
    return {"status": "repaired", "review_id": review_id, "decision_audit": validation.audit}


@router.post("/entities/import-from-drive")
def import_entities_from_drive(
    db: Session = Depends(get_db),
    x_rres_admin_key: str | None = Header(default=None, alias="X-RRES-Admin-Key"),
) -> dict:
    _require_admin_key(x_rres_admin_key)
    folders = DriveService().list_level1_folders()
    result = EntityService().import_entities(db, folders)
    return {"status": "imported", **result, "folders": [folder["name"] for folder in folders]}


@router.get("/gmail/watch/status", response_model=GmailWatchStatusResponse)
def gmail_watch_status(db: Session = Depends(get_db)) -> GmailWatchState | dict:
    state = _latest_watch_state(db)
    return _watch_state_payload(state)


@router.post("/gmail/watch/start", response_model=GmailWatchStartResponse)
def gmail_watch_start(payload: GmailWatchStartRequest, db: Session = Depends(get_db)) -> dict:
    return _start_or_renew_watch(db, payload, message="Gmail watch started.")


@router.post("/gmail/watch/renew", response_model=GmailWatchStartResponse)
def gmail_watch_renew(payload: GmailWatchStartRequest, db: Session = Depends(get_db)) -> dict:
    return _start_or_renew_watch(db, payload, message="Gmail watch renewed.")


@router.post("/gmail/watch/stop", response_model=GmailWatchStatusResponse)
def gmail_watch_stop(db: Session = Depends(get_db)) -> dict:
    GmailService().stop_watch()
    state = _latest_watch_state(db)
    if state:
        state.active = False
        state.last_error = None
        db.add(state)
        db.commit()
        db.refresh(state)
    return _watch_state_payload(state)


def _require_admin_key(value: str | None) -> None:
    settings = get_settings()
    if not settings.admin_api_key:
        raise HTTPException(status_code=403, detail="ADMIN_API_KEY must be configured before repair actions.")
    if value != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid admin API key.")


def _start_or_renew_watch(db: Session, payload: GmailWatchStartRequest, *, message: str) -> dict:
    try:
        state = watch_service.renew_watch(
            db,
            topic_name=payload.topic_name,
            label_ids=payload.label_ids,
            label_filter_behavior=payload.label_filter_behavior,
        )
    except WatchTopicNotConfigured as exc:
        raise HTTPException(
            status_code=422,
            detail="topic_name is required. Provide it in the request body or set GMAIL_PUBSUB_TOPIC_NAME.",
        ) from exc
    except HttpError as exc:
        detail = _google_error_detail(exc)
        raise HTTPException(
            status_code=502,
            detail=(
                "Gmail watch setup failed. The Pub/Sub topic must be in the same "
                "Google Cloud project as the connected OAuth client. Google said: "
                f"{detail}"
            ),
        ) from exc
    return {**_watch_state_payload(state), "message": message}


def _latest_watch_state(db: Session) -> GmailWatchState | None:
    return db.execute(select(GmailWatchState).order_by(GmailWatchState.updated_at.desc())).scalars().first()


def _watch_state_payload(state: GmailWatchState | None) -> dict:
    if not state:
        return {
            "active": False,
            "email_address": None,
            "topic_name": None,
            "label_ids": [],
            "label_filter_behavior": None,
            "history_id": None,
            "expiration_at": None,
            "last_notification_at": None,
            "last_successful_sync_at": None,
            "last_error": None,
        }
    return {
        "active": state.active,
        "email_address": state.email_address,
        "topic_name": state.topic_name,
        "label_ids": state.label_ids or [],
        "label_filter_behavior": state.label_filter_behavior,
        "history_id": state.history_id,
        "expiration_at": state.expiration_at,
        "last_notification_at": state.last_notification_at,
        "last_successful_sync_at": state.last_successful_sync_at,
        "last_error": state.last_error,
    }


def _google_error_detail(exc: HttpError) -> str:
    try:
        payload = exc.content.decode("utf-8") if isinstance(exc.content, bytes) else str(exc.content)
    except Exception:
        payload = str(exc)
    return payload[:1000]


def _processed_filename(db: Session, artifact: FileArtifact) -> str | None:
    if not artifact.file_hash or not artifact.drive_folder_id:
        return artifact.original_filename
    processed = db.execute(
        select(ProcessedFile).where(
            ProcessedFile.file_hash == artifact.file_hash,
            ProcessedFile.drive_folder_id == artifact.drive_folder_id,
        )
    ).scalars().first()
    return processed.filename if processed else artifact.original_filename

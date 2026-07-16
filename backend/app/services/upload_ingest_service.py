"""
Drive upload-folder ingestion.

Files that clients drop into their per-entity "Client Uploads" folder, or that the RRES team drops
into the single root-level "RRES Uploads" folder, are read, classified, named, and MOVED into the
correct destination folder -- the same filing the email pipeline produces, just sourced from Drive.

This reuses the existing email pipeline (pdf -> classify -> validate) by building a synthetic
EmailMessageData + ProcessedEmail per file. It deliberately does NOT call file_email_artifacts:
emails upload generated cover-paged PDFs, whereas an upload MOVES the byte-identical original (with
its real extension) into place per the client's "move the whole file" rule. See
backend/DRIVE_UPLOADS_PLAN.md.
"""
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import FileArtifact, FilingLog, ProcessedEmail, ProcessedFile
from app.services.classifier_service import ClassifierService
from app.services.decision_service import DecisionValidator
from app.services.entity_service import EntityService
from app.services.filing_service import FilingService
from app.services.pdf_service import PdfService
from app.services.processing_service import (
    FINAL_STATUSES,
    _PROCESS_SEMAPHORE,
    ProcessingService,
)
from app.services.types import ClassificationResult, EmailAttachment, EmailMessageData
from app.services.usage_guard import ApiLimitReached
from app.utils.files import ensure_dir, safe_filename
from app.utils.hashing import sha256_file
from app.utils.time import date_prefix, utc_now

logger = logging.getLogger(__name__)

# Synthetic ProcessedEmail.gmail_message_id prefix for Drive uploads. Doubles as the isolation
# marker that keeps upload rows out of the Gmail-only retry/label code paths.
UPLOAD_ID_PREFIX = "drive-upload:"


def upload_message_id(drive_file_id: str) -> str:
    return f"{UPLOAD_ID_PREFIX}{drive_file_id}"


def is_upload_message_id(gmail_message_id: str | None) -> bool:
    return bool(gmail_message_id) and gmail_message_id.startswith(UPLOAD_ID_PREFIX)


# Backwards-compatible aliases: the shared implementations live in app.utils.errors so the email
# pipeline reuses the exact same logic. Kept here as private names so existing call sites/tests
# that import from this module keep working.
from app.utils.errors import api_unavailable_reason as _api_unavailable_reason  # noqa: E402
from app.utils.errors import clean_error_message as _clean_error_message  # noqa: E402
from app.utils.errors import is_api_unavailable_error as _is_api_unavailable_error  # noqa: E402
from app.utils.errors import is_permanent_error as _is_permanent_error  # noqa: E402


class UploadIngestService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.pdf = PdfService()
        self.classifier = ClassifierService()
        self.validator = DecisionValidator()
        self.filing = FilingService()
        self.entities = EntityService()
        # Reuse ProcessingService's artifact-persist + review-creation helpers verbatim so an
        # upload review item is shaped identically to an email one (same dashboard rendering).
        self._proc = ProcessingService()

    def process_drive_upload(
        self,
        db: Session,
        file_meta: dict,
        source_folder_id: str,
        source_kind: str,
        fixed_entity: str | None,
    ) -> dict:
        """
        Classify and file ONE uploaded Drive file.

        file_meta: a Drive files().list() dict (id, name, mimeType, size, createdTime).
        source_kind: "client_uploads" | "rres_uploads".
        fixed_entity: the owning entity for a Client Uploads file (entity is fixed, only the
            category is classified); None for RRES Uploads (entity is classified too).
        """
        file_id = file_meta["id"]
        message_id = upload_message_id(file_id)

        existing = db.execute(
            select(ProcessedEmail).where(ProcessedEmail.gmail_message_id == message_id)
        ).scalars().first()
        # Dedup ("never touch the same file twice"): a final or in-flight row means we're done.
        # NOTE: an in-place EDIT (same Drive id, new content) is intentionally skipped here.
        if existing and existing.status in FINAL_STATUSES | {"pending_review", "processing"}:
            return {"file_id": file_id, "status": existing.status, "email_id": existing.id, "message": "Already processed or in progress."}

        email_row = existing or ProcessedEmail(gmail_message_id=message_id, status="processing")
        email_row.status = "processing"
        email_row.attempts = (email_row.attempts or 0) + 1
        email_row.subject = file_meta.get("name")
        email_row.metadata_json = {
            **(email_row.metadata_json or {}),
            "upload": {
                "source_kind": source_kind,
                "source_folder_id": source_folder_id,
                "drive_file_id": file_id,
                "original_filename": file_meta.get("name"),
                "original_ext": Path(file_meta.get("name") or "").suffix,
                "fixed_entity": fixed_entity,
                "mime_type": file_meta.get("mime_type") or file_meta.get("mimeType"),
            },
        }
        db.add(email_row)
        db.commit()
        db.refresh(email_row)

        _PROCESS_SEMAPHORE.acquire()
        try:
            work_dir = ensure_dir(self.settings.artifact_root_resolved / safe_filename(message_id))
            local_path = self._download(file_id, file_meta.get("name") or file_id, work_dir)

            # Dedup hash is the ORIGINAL file's content (that's what we MOVE), so it stays correct
            # across a later review-approve even after the local temp file is gone. Prefer Drive's
            # md5Checksum (free, in the listing); fall back to hashing the downloaded original.
            meta = email_row.metadata_json or {}
            original_hash = file_meta.get("md5Checksum") or sha256_file(local_path)
            email_row.metadata_json = {
                **meta,
                "upload": {**(meta.get("upload") or {}), "original_hash": original_hash},
            }

            created = self._parse_drive_time(file_meta.get("createdTime"))
            message = self._synthetic_email(message_id, file_meta, local_path, created)
            email_row.received_at = created
            db.commit()

            # Single document, not an email -> build just the one document artifact (no email_body /
            # combined_package / source-note cover). See PdfService.prepare_single_document.
            prepared = self.pdf.prepare_single_document(message, work_dir)
            artifacts = self._proc._persist_artifacts(db, email_row, prepared)
            active_entities = self.entities.list_active(db)
            classification = self.classifier.classify(db, prepared, active_entities)

            # Client Uploads: the folder already tells us the client -> force the entity, keep
            # Claude's category/level3. RRES Uploads: trust Claude's entity as-is.
            #
            # Misfile safety net (Client Uploads only): if Claude confidently read the document as a
            # DIFFERENT *known* entity than the folder owner, the file was very likely dropped in the
            # wrong client's folder. Don't silently misfile -- route it to human review. The folder
            # owner still wins in the normal case (Claude unsure, or agrees, or its guess isn't a
            # known entity).
            force_review_reason = None
            if fixed_entity:
                claude_entity = (classification.entity or "").strip()
                known_names = {e.entity_name for e in active_entities}
                conf = classification.confidence or 0
                if (claude_entity and claude_entity != fixed_entity
                        and claude_entity in known_names
                        and conf >= self.settings.auto_file_confidence):
                    force_review_reason = (
                        f"Uploaded into '{fixed_entity}' but the document content matches a different "
                        f"known client '{claude_entity}' (confidence {conf}%). Possible wrong-folder drop."
                    )
                classification.entity = fixed_entity
                classification.unknown_entity = False
                audit = classification.decision_audit or {}
                audit["upload_fixed_entity"] = fixed_entity
                if force_review_reason:
                    audit["upload_entity_mismatch"] = {"folder_owner": fixed_entity, "content_entity": claude_entity, "confidence": conf}
                classification.decision_audit = audit

            validation = self.validator.validate(
                classification, email_row, prepared.issues, active_entities, artifacts=artifacts,
                force_file=False, allow_new_entity=fixed_entity is None,
            )
            # Override a "file" decision to review when the mismatch net tripped.
            if force_review_reason and validation.final_action == "file":
                validation.final_action = "needs_review"
                validation.decision.needs_review = True
                validation.decision.action = "needs_review"
                validation.decision.needs_review_reason = force_review_reason
                validation.decision.reason = force_review_reason
                validation.audit["reasons"] = [*(validation.audit.get("reasons") or []), "upload_entity_mismatch"]
            email_row.metadata_json = {
                **(email_row.metadata_json or {}),
                "decision_audit": validation.audit,
            }
            db.commit()

            if validation.should_reject or validation.should_review:
                # Uncertain / not-a-filing -> create a review item and LEAVE the file in place.
                review = self._proc._create_review(db, email_row, validation.decision, prepared.issues)
                email_row.status = "pending_review"
                db.add(FilingLog(
                    email_id=email_row.id, subject=email_row.subject,
                    entity=validation.decision.entity, folder_path="Needs Review",
                    confidence=validation.decision.confidence, status="pending_review",
                    message=validation.decision.reason,
                ))
                db.commit()
                return {"file_id": file_id, "status": "pending_review", "email_id": email_row.id, "review_id": review.id, "message": "Created Needs Review item; file left in upload folder."}

            # Confident -> MOVE the original file into its destination, renamed.
            attachment_artifact = next((a for a in artifacts if a.kind == "attachment"), None)
            moved = self._file_upload(db, email_row, validation.decision, attachment_artifact, file_meta, source_folder_id)
            email_row.status = "filed"
            db.commit()
            return {"file_id": file_id, "status": "filed", "email_id": email_row.id, "message": f"Moved to {moved}"}
        except ApiLimitReached as exc:
            # Daily Claude limit hit (e.g. a big first scan). Park as waiting_api_limit so the limit
            # can reset; the upload-retry loop picks it up later. The file is untouched in Drive.
            db.rollback()
            email_row.status = "waiting_api_limit"
            email_row.last_error = str(exc)
            db.commit()
            return {"file_id": file_id, "status": "waiting_api_limit", "email_id": email_row.id, "message": str(exc)}
        except Exception as exc:
            db.rollback()
            clean = _clean_error_message(exc)
            # Claude, Gmail, or Drive unreachable (rate limit, server error/maintenance, network
            # issue, expired/revoked credential, billing) -- not a problem with this file. Pause
            # like our own daily-limit hold instead of "failed" so it self-heals once the
            # provider/credential recovers, rather than silently exhausting the retry ceiling
            # (see app/utils/errors.py).
            if _is_api_unavailable_error(exc):
                try:
                    email_row.status = "waiting_api_limit"
                    email_row.last_error = clean
                    email_row.metadata_json = {**(email_row.metadata_json or {}), "retryable_reason": _api_unavailable_reason(exc)}
                    db.add(FilingLog(email_id=email_row.id, subject=email_row.subject, status="waiting_api_limit", message=clean))
                    db.commit()
                except Exception:
                    logger.exception("Failed to record upload api-unavailable pause for %s", message_id)
                return {"file_id": file_id, "status": "waiting_api_limit", "email_id": email_row.id, "message": clean}
            permanent = _is_permanent_error(exc)
            try:
                if permanent:
                    # A permanent, unretryable problem (e.g. password-protected/corrupt PDF Claude
                    # rejects). Do NOT churn retries -> send to Needs Review with a clear message so
                    # a human unlocks/replaces the file. The file is left in place in Drive.
                    review = self._proc._create_review(db, email_row, self._minimal_decision(clean), [clean])
                    email_row.status = "pending_review"
                    email_row.last_error = clean
                    db.add(FilingLog(email_id=email_row.id, subject=email_row.subject, folder_path="Needs Review",
                                     status="pending_review", message=clean))
                    db.commit()
                    return {"file_id": file_id, "status": "pending_review", "email_id": email_row.id, "review_id": review.id, "message": clean}
                # Transient/unknown -> failed (the retry loop will re-attempt up to the cap).
                email_row.status = "failed"
                email_row.last_error = clean
                db.add(FilingLog(email_id=email_row.id, subject=email_row.subject, status="failed", message=clean))
                db.commit()
            except Exception:
                logger.exception("Failed to record upload outcome for %s", message_id)
            if permanent:
                return {"file_id": file_id, "status": "pending_review", "email_id": email_row.id, "message": clean}
            raise
        finally:
            _PROCESS_SEMAPHORE.release()

    def _minimal_decision(self, reason: str) -> ClassificationResult:
        # A bare needs_review decision used when a permanent error prevents classification, so the
        # Needs Review item still renders with a clear reason for the human.
        return ClassificationResult(
            entity=None, level2=None, level3=None, file_summary="Upload needs attention",
            confidence=0, unknown_entity=True, needs_review=True, reason=reason,
            action="needs_review", needs_review_reason=reason, urgent=True,
        )

    def _file_upload(
        self,
        db: Session,
        email_row: ProcessedEmail,
        decision,
        artifact: FileArtifact | None,
        file_meta: dict,
        source_folder_id: str,
    ) -> str:
        # Resolve the destination folder exactly like email filing, then MOVE the original Drive
        # file there (byte-identical) renamed to the standard format with its ORIGINAL extension.
        level2, level3 = self.filing._attachment_target_spec(decision, artifact) if artifact else (decision.level2, decision.level3)
        dest_folder_id, folder_path = self.filing.drive.resolve_target_folder(db, decision.entity, level2, level3)

        prefix = (decision.decision_audit or {}).get("filename_date") or date_prefix(email_row.received_at)
        summary = self.filing._summary_for_artifact(decision, artifact) if artifact else decision.file_summary
        summary = safe_filename(self.filing._trim_redundant_date(summary))
        ext = Path(file_meta.get("name") or "").suffix or ".pdf"
        new_name = safe_filename(f"{prefix} - {summary}") + ext

        file_id = file_meta["id"]
        # Dedup on the ORIGINAL file's content hash (persisted in metadata at scan time), since the
        # original is what gets moved. Survives a later review-approve when the temp file is gone.
        file_hash = ((email_row.metadata_json or {}).get("upload") or {}).get("original_hash")

        # Dedup against an already-filed identical doc in this destination.
        existing = self.filing.drive.find_valid_processed_file(db, file_hash, dest_folder_id) if file_hash else None
        if existing:
            # The content is ALREADY filed correctly in the destination. Do NOT move a second copy
            # in (that would duplicate it). Instead trash the redundant original so the upload
            # folder is cleared, and point our records at the existing filed copy.
            self.filing.drive.trash_file(file_id)
            status = "duplicate"
            drive_link = f"https://drive.google.com/file/d/{existing.drive_file_id}/view"
            dest_file_id = existing.drive_file_id
        else:
            moved = self.filing.drive.move_file(file_id, source_folder_id, dest_folder_id, new_name)
            drive_link = moved.get("webViewLink")
            dest_file_id = file_id
            if file_hash:
                db.add(ProcessedFile(
                    file_hash=file_hash, drive_folder_id=dest_folder_id,
                    drive_file_id=file_id, filename=new_name, source_email_id=email_row.id,
                ))
            status = "filed"

        if artifact:
            artifact.drive_file_id = dest_file_id
            artifact.drive_link = drive_link
            artifact.drive_folder_id = dest_folder_id
            artifact.status = status
            artifact.metadata_json = {**(artifact.metadata_json or {}), "folder_path": folder_path}
        db.add(FilingLog(
            email_id=email_row.id, subject=email_row.subject, entity=decision.entity,
            folder_path=folder_path, confidence=decision.confidence, drive_link=drive_link,
            status=status, message=decision.reason,
        ))
        return folder_path

    def _download(self, file_id: str, filename: str, work_dir: Path) -> Path:
        original_dir = ensure_dir(work_dir / "original")
        local_path = original_dir / safe_filename(filename)
        with local_path.open("wb") as handle:
            for chunk in self.filing.drive.download_file_stream(file_id):
                handle.write(chunk)
        return local_path

    def _synthetic_email(self, message_id: str, file_meta: dict, local_path: Path, created) -> EmailMessageData:
        # A one-attachment "email" with no sender/subject/body. The pipeline is null-safe for these
        # (classifier_service._contact_hints uses `or ""`), so classification runs on the file's own
        # filename + content -- exactly what we want for an uploaded document.
        attachment = EmailAttachment(
            filename=file_meta.get("name") or "upload",
            mime_type=file_meta.get("mime_type") or file_meta.get("mimeType"),
            local_path=local_path,
            size_bytes=int(file_meta.get("size") or local_path.stat().st_size),
        )
        return EmailMessageData(
            gmail_message_id=message_id,
            thread_id=None,
            sender=None,
            recipient=None,
            subject=None,
            received_at=created,
            body_text="",
            body_html=None,
            attachments=[attachment],
            raw_metadata={"source": "drive_upload", "drive_file_id": file_meta.get("id")},
        )

    @staticmethod
    def _parse_drive_time(value: str | None):
        if not value:
            return utc_now()
        from datetime import datetime, timezone

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return utc_now()

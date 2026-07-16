import dataclasses
import logging
import threading
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import FileArtifact, FilingLog, NeedsReview, ProcessedEmail
from app.services.classifier_service import ClassifierService
from app.services.decision_service import DecisionValidation, DecisionValidator
from app.services.entity_service import EntityService
from app.services.filing_service import FilingService, apply_decorative_flags
from app.services.gmail_service import GmailService
from app.services.pdf_service import PdfService
from app.services.types import ClassificationResult, PreparedEmail
from app.services.usage_guard import ApiLimitReached
from app.utils.errors import api_unavailable_reason, clean_error_message, is_api_unavailable_error, is_permanent_error
from app.utils.files import ensure_dir
from app.utils.time import as_utc, utc_now


logger = logging.getLogger(__name__)

FINAL_STATUSES = {"filed", "approved", "corrected", "rejected", "skipped"}
RETRYABLE_STATUSES = {"waiting_api_limit"}

# Shared with main.py's retry loop and the /admin/failed-emails diagnostic endpoint, so all three
# agree on when a 'failed' row (a per-email problem) is considered permanently stuck vs still
# auto-retrying. 'waiting_api_limit' rows (account/API-level, not this email's fault) are NOT
# subject to this ceiling -- see main.py's retry-loop query.
MAX_AUTO_RETRY_ATTEMPTS = 8

# If an email has been in "processing" status for longer than this, assume the
# previous worker crashed mid-flight and allow a retry.
_PROCESSING_STALE_AFTER = timedelta(minutes=10)

# Bounds how many emails do heavy Claude+Drive work at once (per worker process), so a
# Pub/Sub backlog flushing all at once doesn't burst both APIs into rate limits / 5xx.
_PROCESS_SEMAPHORE = threading.BoundedSemaphore(max(1, get_settings().process_max_concurrent))


class ProcessingService:
    # Process-wide debounce so a batch of unmatched emails triggers at most one Drive sync.
    _last_entity_sync = None
    ENTITY_SYNC_COOLDOWN_SECONDS = 60

    # Serializes the on-demand entity reconcile across worker threads so a burst of
    # unknown-entity emails coalesces into ONE Drive folder-list (no thundering herd) and the
    # heavyweight registry import can never run concurrently. Followers re-read the registry under
    # the lock and see the first thread's import, so they skip the Drive call entirely.
    _reconcile_lock = threading.Lock()
    _last_reconcile_attempt = None
    RECONCILE_COOLDOWN_SECONDS = 15

    def __init__(self) -> None:
        self.settings = get_settings()
        self.gmail = GmailService()
        self.pdf = PdfService()
        self.classifier = ClassifierService()
        self.validator = DecisionValidator()
        self.filing = FilingService()
        self.entities = EntityService()

    def process_message(self, db: Session, gmail_message_id: str) -> dict:
        existing = db.execute(
            select(ProcessedEmail).where(ProcessedEmail.gmail_message_id == gmail_message_id)
        ).scalars().first()
        if existing and existing.status in FINAL_STATUSES | {"pending_review"}:
            return {"gmail_message_id": gmail_message_id, "status": existing.status, "email_id": existing.id, "message": "Already processed or waiting for review."}
        if existing and existing.status == "processing":
            age = utc_now() - as_utc(existing.updated_at)
            if age < _PROCESSING_STALE_AFTER:
                logger.info("Message %s is already being processed (age %s); skipping duplicate", gmail_message_id, age)
                return {"gmail_message_id": gmail_message_id, "status": "processing", "email_id": existing.id, "message": "Already in progress."}

        email_row = existing or ProcessedEmail(gmail_message_id=gmail_message_id, status="processing")
        email_row.attempts = (email_row.attempts or 0) + 1
        db.add(email_row)
        db.commit()
        db.refresh(email_row)

        # Throttle the heavy work (after the cheap dedup checks above) to cap API burst.
        _PROCESS_SEMAPHORE.acquire()
        try:
            work_dir = ensure_dir(self.settings.artifact_root_resolved / gmail_message_id)
            message = self.gmail.fetch_message(gmail_message_id, work_dir)
            email_row.thread_id = message.thread_id
            email_row.sender = message.sender
            email_row.subject = message.subject
            email_row.received_at = message.received_at
            email_row.metadata_json = {
                **message.raw_metadata,
                "real_attachment_count": len(message.attachments),
                "inline_asset_count": len(message.inline_assets),
                "inline_assets": [
                    {
                        "filename": asset.filename,
                        "content_id": asset.content_id,
                        "mime_type": asset.mime_type,
                        "size_bytes": asset.size_bytes,
                        "width": asset.width,
                        "height": asset.height,
                        "part_classification_reason": asset.part_classification_reason,
                    }
                    for asset in message.inline_assets
                ],
            }
            db.commit()

            prepared = self.pdf.prepare_email(message, work_dir)
            artifacts = self._persist_artifacts(db, email_row, prepared)
            active_entities = self.entities.list_active(db)
            classification = self.classifier.classify(db, prepared, active_entities)
            pristine = dataclasses.replace(classification)
            # Stamp agreed-decorative images (part-classifier suspicion + Claude confirmation)
            # BEFORE validation so the multi-entity gate, Needs-Review staging, and filing all
            # see the verdict. Committed with the decision_audit below.
            apply_decorative_flags(classification.decision_audit, artifacts)
            validation = self.validator.validate(classification, email_row, prepared.issues, active_entities, artifacts=artifacts)
            # On-demand Drive reconcile: if (and only if) this email is heading to review because
            # Claude named an entity not yet in the registry, sync that folder in from Drive and
            # retry. Known-entity emails never touch Drive. Concurrency-safe + burst-coalesced.
            classification, active_entities, validation = self._reconcile_unknown_entities(
                db, prepared, email_row, artifacts, classification, pristine, active_entities, validation
            )
            email_row.metadata_json = {
                **(email_row.metadata_json or {}),
                "decision_audit": validation.audit,
            }
            db.commit()

            if validation.should_reject:
                self._reject_email(db, email_row, artifacts, validation)
                try:
                    self.gmail.mark_skipped(gmail_message_id)
                except Exception as exc:
                    email_row.last_error = f"Rejected, but Gmail skipped/read label failed: {exc}"
                    db.commit()
                return {"gmail_message_id": gmail_message_id, "status": "rejected", "email_id": email_row.id, "message": "Rejected as non-filing email."}

            if validation.should_review:
                review = self._create_review(db, email_row, validation.decision, prepared.issues)
                if self.settings.drive_root_id:
                    try:
                        self.filing.file_to_needs_review_folder(db, email_row, artifacts)
                    except Exception as exc:
                        review.metadata_json = {
                            **(review.metadata_json or {}),
                            "needs_review_drive_upload_error": str(exc),
                        }
                email_row.status = "pending_review"
                db.add(FilingLog(email_id=email_row.id, sender=email_row.sender, subject=email_row.subject, entity=validation.decision.entity, folder_path="Needs Review", confidence=validation.decision.confidence, status="pending_review", message=validation.decision.reason, processing_time_ms=None))
                db.commit()
                return {"gmail_message_id": gmail_message_id, "status": "pending_review", "email_id": email_row.id, "review_id": review.id, "message": "Created Needs Review item."}

            self.filing.file_email_artifacts(db, email_row, validation.decision, artifacts)
            email_row.status = "filed"
            db.commit()
            try:
                self.gmail.mark_filed(gmail_message_id)
            except Exception as exc:
                email_row.last_error = f"Filed, but Gmail label/read failed: {exc}"
                db.commit()
            return {"gmail_message_id": gmail_message_id, "status": "filed", "email_id": email_row.id, "message": "Filed successfully."}
        except ApiLimitReached as exc:
            return self._pause_for_api_limit(db, email_row, exc)
        except Exception as exc:
            # Claude, Gmail, or Drive being unreachable (rate limited, server error/maintenance,
            # network issue, expired/revoked credential, out of credits) means the ACCOUNT/API is
            # blocked, not that anything is wrong with this email -- every email in flight fails
            # identically, and it resolves itself once the provider/billing/credential recovers.
            # Pause it like our own internal daily-limit hold (same 2h backoff, no attempt ceiling)
            # instead of "failed" so it self-heals instead of silently exhausting the retry
            # ceiling and needing a manual reset.
            if is_api_unavailable_error(exc):
                return self._pause_for_api_unavailable(db, email_row, exc)
            # Clean, human-readable message instead of the raw provider blob; and don't churn
            # retries on a PERMANENT problem with the file itself (e.g. a password-protected /
            # corrupt attachment) -- flag it so the retry loop skips it.
            clean = clean_error_message(exc)
            permanent = is_permanent_error(exc)
            email_row.status = "failed"
            email_row.last_error = clean
            if permanent:
                email_row.metadata_json = {**(email_row.metadata_json or {}), "permanent_failure": True}
            db.add(FilingLog(email_id=email_row.id, sender=email_row.sender, subject=email_row.subject, status="failed", message=clean))
            db.commit()
            try:
                self.gmail.mark_failed(gmail_message_id)
            except Exception:
                pass
            raise
        finally:
            _PROCESS_SEMAPHORE.release()

    def process_unread(self, db: Session, limit: int, newer_than_minutes: int | None) -> dict:
        ids = self.gmail.search_unread(limit=limit, newer_than_minutes=newer_than_minutes)
        results = []
        skipped = 0
        for message_id in ids:
            try:
                results.append(self.process_message(db, message_id))
            except Exception as exc:
                skipped += 1
                results.append({"gmail_message_id": message_id, "status": "failed", "message": str(exc)})
        processed_count = len([item for item in results if item.get("status") not in {"failed", "waiting_api_limit"}])
        waiting_count = len([item for item in results if item.get("status") == "waiting_api_limit"])
        return {
            "processed_count": processed_count,
            "skipped_count": skipped,
            "waiting_count": waiting_count,
            "results": results,
        }

    @staticmethod
    def _unknown_proposed_entities(classification: ClassificationResult, active_entities: list) -> set[str]:
        # Every entity name Claude referenced for this email -- the email-level entity, any
        # additional_entities, and each per-attachment entity -- that is NOT already in the
        # registry. Drives the on-demand Drive sync: empty set (all known) => no Drive call.
        known = {e.entity_name for e in active_entities}
        names: set[str] = set()
        if classification.entity:
            names.add(classification.entity)
        audit = classification.decision_audit or {}
        for name in audit.get("additional_entities") or []:
            if name:
                names.add(str(name))
        for value in (audit.get("artifact_classifications") or {}).values():
            if isinstance(value, dict) and value.get("entity"):
                names.add(str(value["entity"]))
        return {name for name in names if name and name not in known}

    def _reconcile_unknown_entities(
        self,
        db: Session,
        prepared: PreparedEmail,
        email_row: ProcessedEmail,
        artifacts: list[FileArtifact],
        classification: ClassificationResult,
        pristine: ClassificationResult,
        active_entities: list,
        validation: DecisionValidation,
    ) -> tuple[ClassificationResult, list, DecisionValidation]:
        # Pull a freshly-created Drive folder into the registry ON DEMAND so a multi-entity email
        # (up to the 3-entity cap) whose entities include a just-created one still auto-files,
        # WITHOUT syncing Drive for already-known entities. Returns the (possibly updated)
        # (classification, active_entities, validation).
        if not validation.should_review:
            return classification, active_entities, validation
        if not (self.settings.enable_real_google and self.settings.drive_root_id):
            return classification, active_entities, validation
        unknown_initial = self._unknown_proposed_entities(classification, active_entities)
        if not unknown_initial:
            # Every entity Claude named is already known -> no Drive call at all.
            return classification, active_entities, validation

        with ProcessingService._reconcile_lock:
            # End our read transaction so we observe an import another worker just committed
            # (SQLite snapshot isolation would otherwise hide it).
            db.commit()
            active_entities = self.entities.list_active(db)
            if self._unknown_proposed_entities(classification, active_entities):
                # Still unknown after re-reading -> we are the thread that hits Drive, unless a
                # reconcile ran moments ago and these folders still weren't there (genuinely new).
                now = utc_now()
                last = ProcessingService._last_reconcile_attempt
                if not (last and (now - last).total_seconds() < self.RECONCILE_COOLDOWN_SECONDS):
                    ProcessingService._last_reconcile_attempt = now
                    try:
                        folders = self.filing.drive.list_level1_folders()
                        result = self.entities.import_entities(db, folders)
                        if result.get("created") or result.get("updated"):
                            logger.info("Entity registry reconciled from Drive: %s", result)
                        active_entities = self.entities.list_active(db)
                    except Exception as exc:
                        logger.warning("Entity reconcile sync failed; using existing registry: %s", exc)

        # Did the registry gain any entity this email actually needed? If not, leave the review
        # decision untouched (the folder genuinely does not exist yet).
        if not (unknown_initial & {e.entity_name for e in active_entities}):
            return classification, active_entities, validation

        # Cheap path first: re-validate the ORIGINAL proposal against the refreshed registry
        # (no Claude call). Enough when Claude already proposed the entity confidently.
        classification = dataclasses.replace(pristine)
        validation = self.validator.validate(classification, email_row, prepared.issues, active_entities, artifacts=artifacts)
        # Re-classify exactly once only if every named entity is now known AND review still stands
        # (Claude was unsure only because it classified before the folder existed, e.g. a
        # per-attachment entity in a multi-entity email). Skipped when an unknown remains, since
        # that would route to review regardless -- so no wasted Claude call.
        if validation.should_review and not self._unknown_proposed_entities(classification, active_entities):
            classification = self.classifier.classify(db, prepared, active_entities)
            validation = self.validator.validate(classification, email_row, prepared.issues, active_entities, artifacts=artifacts)
        return classification, active_entities, validation

    def _sync_entities_from_drive(self, db: Session, *, force: bool = False) -> bool:
        # Lazy, debounced sync of the entity registry from the Drive master folders. Called
        # only when an email fails to match (a folder may exist in Drive but not yet locally).
        # Returns True if a sync was actually attempted this call. Guarded: skipped offline/mock,
        # rate-limited by a process-wide cooldown, and a Drive hiccup never blocks processing.
        if not self.settings.enable_real_google or not self.settings.drive_root_id:
            return False
        now = utc_now()
        last = ProcessingService._last_entity_sync
        if not force and last and (now - last).total_seconds() < self.ENTITY_SYNC_COOLDOWN_SECONDS:
            return False
        ProcessingService._last_entity_sync = now  # debounce attempts regardless of outcome
        try:
            folders = self.filing.drive.list_level1_folders()
            result = self.entities.import_entities(db, folders)
            if result.get("created") or result.get("updated"):
                logger.info("Entity registry synced from Drive: %s", result)
            return True
        except Exception as exc:
            logger.warning("Entity sync from Drive failed; using existing registry. Reason: %s", exc)
            return False

    def _pause_for_api_limit(self, db: Session, email_row: ProcessedEmail, exc: ApiLimitReached) -> dict:
        email_row.status = "waiting_api_limit"
        email_row.last_error = str(exc)
        email_row.metadata_json = {
            **(email_row.metadata_json or {}),
            "retryable_reason": "api_limit",
            "api_limit": {
                "provider": exc.provider,
                "used": exc.used,
                "limit": exc.limit,
            },
        }
        db.add(
            FilingLog(
                email_id=email_row.id,
                sender=email_row.sender,
                subject=email_row.subject,
                status="waiting_api_limit",
                message=str(exc),
            )
        )
        db.commit()
        return {
            "gmail_message_id": email_row.gmail_message_id,
            "status": "waiting_api_limit",
            "email_id": email_row.id,
            "message": str(exc),
        }

    def _pause_for_api_unavailable(self, db: Session, email_row: ProcessedEmail, exc: Exception) -> dict:
        # Same treatment as _pause_for_api_limit: the account/API, not this email, is the problem.
        # Reuses "waiting_api_limit" status (and its ceiling-free, 2h-backoff retry handling) so
        # every email affected by an Anthropic-side outage recovers automatically once it clears.
        clean = clean_error_message(exc)
        email_row.status = "waiting_api_limit"
        email_row.last_error = clean
        email_row.metadata_json = {**(email_row.metadata_json or {}), "retryable_reason": api_unavailable_reason(exc)}
        db.add(
            FilingLog(
                email_id=email_row.id,
                sender=email_row.sender,
                subject=email_row.subject,
                status="waiting_api_limit",
                message=clean,
            )
        )
        db.commit()
        return {
            "gmail_message_id": email_row.gmail_message_id,
            "status": "waiting_api_limit",
            "email_id": email_row.id,
            "message": clean,
        }

    def _persist_artifacts(self, db: Session, email_row: ProcessedEmail, prepared: PreparedEmail) -> list[FileArtifact]:
        existing = db.query(FileArtifact).filter(FileArtifact.email_id == email_row.id).all()
        if existing:
            return existing
        artifacts: list[FileArtifact] = []
        for item in prepared.artifacts:
            artifact = FileArtifact(
                email_id=email_row.id,
                kind=item.kind,
                original_filename=item.original_filename,
                local_path=str(item.local_path),
                generated_pdf_path=str(item.generated_pdf_path) if item.generated_pdf_path else None,
                mime_type=item.mime_type,
                file_hash=item.file_hash,
                size_bytes=item.size_bytes,
                status="unsupported" if item.issue else "prepared",
                metadata_json={
                    "text_preview": item.text_preview,
                    "requires_claude_pdf": item.requires_claude_pdf,
                    "issue": item.issue,
                    # Part-classifier's "probably a signature logo" suspicion; combined with
                    # Claude's per-attachment `decorative` verdict by apply_decorative_flags.
                    "ambiguous_image": getattr(item, "ambiguous_image", False),
                },
            )
            db.add(artifact)
            artifacts.append(artifact)
        db.commit()
        return artifacts

    def _create_review(
        self,
        db: Session,
        email_row: ProcessedEmail,
        classification: ClassificationResult,
        issues: list[str],
    ) -> NeedsReview:
        review = NeedsReview(
            email_id=email_row.id,
            proposed_entity=classification.entity,
            proposed_level2=classification.level2,
            proposed_level3=classification.level3,
            proposed_file_summary=classification.file_summary,
            claude_reasoning=classification.reason,
            confidence=classification.confidence,
            urgent=classification.urgent,
            status="pending",
            metadata_json={
                "issues": issues,
                "unknown_entity": classification.unknown_entity,
                "proposed_action": classification.action,
                "proposed_document_date": classification.document_date,
                "needs_review_reason": classification.needs_review_reason,
                "decision_audit": classification.decision_audit,
            },
        )
        db.add(review)
        db.commit()
        db.refresh(review)
        return review

    def _reject_email(
        self,
        db: Session,
        email_row: ProcessedEmail,
        artifacts: list[FileArtifact],
        validation: DecisionValidation,
    ) -> None:
        for artifact in artifacts:
            artifact.status = "rejected"
        email_row.status = "rejected"
        email_row.metadata_json = {
            **(email_row.metadata_json or {}),
            "decision_audit": validation.audit,
        }
        db.add(
            FilingLog(
                email_id=email_row.id,
                sender=email_row.sender,
                subject=email_row.subject,
                entity=validation.decision.entity,
                folder_path=None,
                confidence=validation.decision.confidence,
                status="rejected",
                message=validation.decision.reason or validation.decision.needs_review_reason,
            )
        )
        db.commit()

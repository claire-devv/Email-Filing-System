from datetime import timezone

from sqlalchemy.orm import Session

from app.db.models import FileArtifact, FilingLog, NeedsReview, ProcessedEmail, utc_now
from app.services.decision_service import DecisionValidator
from app.services.entity_service import EntityService
from app.services.filing_service import FilingService, is_decorative_artifact
from app.services.gmail_service import GmailService
from app.services.learning_service import LearningService
from app.services.types import ClassificationResult
from app.services.upload_ingest_service import is_upload_message_id


class ReviewService:
    def __init__(self) -> None:
        self.filing = FilingService()
        self.learning = LearningService()
        self.validator = DecisionValidator()
        self.entities = EntityService()
        self.gmail = GmailService()

    def approve(self, db: Session, item: NeedsReview, reviewed_by: str | None = None) -> NeedsReview:
        # Read before _store_final_metadata overwrites metadata_json["decision_audit"].
        decision_audit = (item.metadata_json or {}).get("decision_audit", {})
        learn_signals = decision_audit.get("learn_signals")
        original_sender_email = decision_audit.get("original_sender_email")
        classification = ClassificationResult(
            entity=item.proposed_entity,
            level2=item.proposed_level2,
            level3=item.proposed_level3,
            file_summary=item.proposed_file_summary or "Approved Filing Document",
            confidence=item.confidence or 0,
            unknown_entity=False,
            needs_review=False,
            reason="Approved by reviewer.",
            action="file",
            document_date=(item.metadata_json or {}).get("proposed_document_date"),
        )
        validation = self.validator.validate(
            classification,
            item.email,
            (item.metadata_json or {}).get("issues", []),
            self.entities.list_active(db),
            allow_new_entity=False,
            force_file=True,
        )
        if not validation.should_file:
            raise ValueError(f"Cannot approve invalid proposal. Use Correct. Reasons: {', '.join(validation.reasons)}")
        artifacts = db.query(FileArtifact).filter(FileArtifact.email_id == item.email_id).all()
        self._file_review(db, item, validation.decision, artifacts)
        self._store_final_metadata(item, validation.decision, validation.audit)
        self._complete(db, item, "approved", reviewed_by)
        self._learn(
            db, item, learn_signals, original_sender_email, validation.decision, "review_approve"
        )
        self._mark_gmail_filed(item)
        db.commit()
        return item

    def correct(
        self,
        db: Session,
        item: NeedsReview,
        entity: str,
        level2: str,
        level3: str | None,
        file_summary: str,
        document_date: str | None = None,
        reviewed_by: str | None = None,
        alias: str | None = None,
        notes: str | None = None,
        learn: bool = True,
    ) -> NeedsReview:
        # Read before _store_final_metadata overwrites metadata_json["decision_audit"].
        decision_audit = (item.metadata_json or {}).get("decision_audit", {})
        learn_signals = decision_audit.get("learn_signals")
        original_sender_email = decision_audit.get("original_sender_email")
        classification = self.validator.from_review_values(
            entity=entity,
            level2=level2,
            level3=level3 or None,
            file_summary=file_summary,
            confidence=100,
            reason="Corrected by reviewer.",
            document_date=document_date,
        )
        validation = self.validator.validate(
            classification,
            item.email,
            [],
            self.entities.list_active(db),
            allow_new_entity=True,
            force_file=True,
        )
        if not validation.should_file:
            raise ValueError(f"Corrected decision is invalid. Reasons: {', '.join(validation.reasons)}")
        artifacts = db.query(FileArtifact).filter(FileArtifact.email_id == item.email_id).all()
        self._file_review(db, item, validation.decision, artifacts)
        item.corrected_entity = validation.decision.entity
        item.corrected_level2 = validation.decision.level2
        item.corrected_level3 = validation.decision.level3
        item.corrected_file_summary = validation.decision.file_summary
        note = (notes or "").strip() or None
        item.metadata_json = {**(item.metadata_json or {}), "corrected_document_date": document_date, "review_note": note}
        self._store_final_metadata(item, validation.decision, validation.audit)
        self._store_negative_signal(item)
        self._complete(db, item, "corrected", reviewed_by)
        if alias:
            self.entities.add_alias(db, validation.decision.entity, alias)
        if learn:
            self._learn(db, item, learn_signals, original_sender_email, validation.decision, "review_correct", note=note)
        self._mark_gmail_filed(item)
        db.commit()
        return item

    def file_split(
        self,
        db: Session,
        item: NeedsReview,
        assignments: list,
        document_date: str | None = None,
        reviewed_by: str | None = None,
    ) -> NeedsReview:
        # Multi-entity split: file each attachment to its OWN entity/folder per the reviewer's
        # assignments, and copy the combined email PDF into every involved entity's Communications.
        # Reuses file_email_artifacts via a synthetic per-attachment audit, so the existing
        # move/rename/dedup/idempotency and combined-PDF fan-out all apply -- and the staged copies
        # in the Needs Review folder are MOVED out automatically.
        artifacts = db.query(FileArtifact).filter(FileArtifact.email_id == item.email_id).all()
        by_id = {a.id: a for a in artifacts}
        # Agreed-decorative signature/logo images are hidden from the split UI and never filed
        # standalone (they live inside the archived email PDF), so they don't need an assignment.
        uploadable = [
            a for a in artifacts
            if a.kind == "attachment" and a.status != "unsupported" and not is_decorative_artifact(a)
        ]
        if not assignments:
            raise ValueError("No attachment assignments were provided.")
        assigned_ids = {a.artifact_id for a in assignments}
        missing = [a for a in uploadable if a.id not in assigned_ids]
        if missing:
            names = ", ".join((a.original_filename or a.kind) for a in missing)
            raise ValueError(f"Every attachment must be assigned an entity. Unassigned: {names}")

        known_entities = self.entities.list_active(db)
        classifications: dict[str, dict] = {}
        summaries: dict[str, str] = {}
        involved_order: list[str] = []
        filename_date: str | None = None
        normalized_date: str | None = None
        new_entities: set[str] = set()

        for assignment in assignments:
            artifact = by_id.get(assignment.artifact_id)
            if not artifact:
                raise ValueError(f"Attachment {assignment.artifact_id} is not part of this review item.")
            decision = self.validator.from_review_values(
                entity=assignment.entity,
                level2=assignment.level2,
                level3=(assignment.level3 or None),
                file_summary=assignment.file_summary or "Filing Document",
                document_date=document_date,
                reason="Split-filed by reviewer.",
            )
            validation = self.validator.validate(
                decision, item.email, [], known_entities, allow_new_entity=True, force_file=True
            )
            if not validation.should_file:
                label = artifact.original_filename or artifact.kind
                raise ValueError(f"Assignment for '{label}' is invalid: {', '.join(validation.reasons)}")
            key = self._artifact_key(artifact)
            classifications[key] = {
                "entity": validation.decision.entity,
                "level2": validation.decision.level2,
                "level3": validation.decision.level3,
                "entity_confidence": 100,
            }
            summaries[key] = validation.decision.file_summary
            if validation.decision.entity not in involved_order:
                involved_order.append(validation.decision.entity)
            if validation.audit.get("new_entity_requested"):
                new_entities.add(validation.decision.entity)
            filename_date = validation.audit.get("filename_date") or filename_date
            normalized_date = validation.decision.document_date or normalized_date

        primary = involved_order[0]
        primary_key = self._artifact_key(by_id[assignments[0].artifact_id])
        prior_audit = (item.metadata_json or {}).get("decision_audit", {})
        audit = {
            "artifact_classifications": classifications,
            "artifact_summaries": summaries,
            "filename_date": filename_date,
            "email_sender": prior_audit.get("email_sender"),
            "auto_split_entities": sorted(set(involved_order)),
            "split_filed": True,
        }
        synthetic = ClassificationResult(
            entity=primary,
            level2=classifications[primary_key]["level2"],
            level3=classifications[primary_key]["level3"],
            file_summary=summaries[primary_key],
            confidence=100,
            unknown_entity=False,
            needs_review=False,
            reason="Split-filed by reviewer.",
            action="file",
            document_date=normalized_date,
            decision_audit=audit,
        )
        self.filing.file_email_artifacts(db, item.email, synthetic, artifacts)

        item.corrected_entity = primary
        item.corrected_level2 = classifications[primary_key]["level2"]
        item.corrected_level3 = classifications[primary_key]["level3"]
        item.corrected_file_summary = summaries[primary_key]
        item.metadata_json = {
            **(item.metadata_json or {}),
            "corrected_document_date": document_date,
            "split": [
                {
                    "artifact_id": a.artifact_id,
                    "entity": classifications[self._artifact_key(by_id[a.artifact_id])]["entity"],
                    "level2": classifications[self._artifact_key(by_id[a.artifact_id])]["level2"],
                    "level3": classifications[self._artifact_key(by_id[a.artifact_id])]["level3"],
                    "file_summary": summaries[self._artifact_key(by_id[a.artifact_id])],
                }
                for a in assignments
            ],
        }
        self._store_final_metadata(item, synthetic, audit)
        self._complete(db, item, "corrected", reviewed_by)
        # v1: no learned mappings -- email-level signals can't be attributed to one of N entities.
        self._mark_gmail_filed(item)
        db.commit()
        return item

    def _artifact_key(self, artifact: FileArtifact) -> str:
        # Key the synthetic audit the same way filing's match_artifact_classification looks it up:
        # by original filename (its first non-kind match), falling back to kind.
        return artifact.original_filename or artifact.kind

    def _file_review(self, db: Session, item: NeedsReview, decision: ClassificationResult, artifacts: list) -> None:
        # Email items file via the normal artifact pipeline. Drive-upload items instead MOVE the
        # byte-identical original file out of its upload folder into the destination (the upload
        # was never copied into Needs Review -- it was left in place), per DRIVE_UPLOADS_PLAN.md.
        if not is_upload_message_id(item.email.gmail_message_id):
            self.filing.file_email_artifacts(db, item.email, decision, artifacts)
            return
        from app.services.upload_ingest_service import UploadIngestService

        upload = (item.email.metadata_json or {}).get("upload") or {}
        file_meta = {
            "id": upload.get("drive_file_id"),
            "name": upload.get("original_filename"),
            "mimeType": upload.get("mime_type"),
        }
        attachment = next((a for a in artifacts if a.kind == "attachment"), None)
        UploadIngestService()._file_upload(
            db, item.email, decision, attachment, file_meta, upload.get("source_folder_id")
        )

    def reject(self, db: Session, item: NeedsReview, reason: str | None, reviewed_by: str | None = None) -> NeedsReview:
        item.metadata_json = {**(item.metadata_json or {}), "reject_reason": reason}
        artifacts = db.query(FileArtifact).filter(FileArtifact.email_id == item.email_id).all()
        for artifact in artifacts:
            artifact.status = "rejected"
        db.add(
            FilingLog(
                email_id=item.email_id,
                sender=item.email.sender,
                subject=item.email.subject,
                entity=item.proposed_entity,
                folder_path=None,
                confidence=item.confidence,
                status="rejected",
                message=reason or "Rejected by reviewer.",
            )
        )
        self._complete(db, item, "rejected", reviewed_by)
        self._mark_gmail_skipped(item)
        db.commit()
        return item

    def _learn(
        self,
        db: Session,
        item: NeedsReview,
        learn_signals: list[dict] | None,
        original_sender_email: str | None,
        decision: ClassificationResult,
        source: str,
        note: str | None = None,
    ) -> None:
        # Preferred path: record every typed signal captured at classification time (address,
        # org, each non-forwarder participant email/domain, plus keyword fallbacks) so one
        # correction generalizes across subjects, properties, and senders. Falls back to the
        # legacy sender+keyword learning for items filed before signals were captured.
        if learn_signals:
            self.learning.record_signals(
                db, learn_signals, decision.entity, decision.level2, decision.level3, source, note=note
            )
            return
        self.learning.record_review_mapping(
            db, item.email.sender, item.email.subject, decision.entity, decision.level2, decision.level3, source, note=note
        )
        if original_sender_email:
            self.learning.record_sender_mapping(
                db, original_sender_email, decision.entity, decision.level2, decision.level3, f"{source}_forwarded", note=note
            )

    def _complete(self, db: Session, item: NeedsReview, decision: str, reviewed_by: str | None) -> None:
        item.status = decision
        item.reviewer_decision = decision
        item.reviewed_by = reviewed_by
        item.reviewed_at = utc_now()
        item.email.status = decision
        db.add(item)

    def _mark_gmail_filed(self, item: NeedsReview) -> None:
        # Drive-upload items have no Gmail message to label -- skip (their id is synthetic).
        if is_upload_message_id(item.email.gmail_message_id):
            return
        try:
            self.gmail.mark_filed(item.email.gmail_message_id)
        except Exception as exc:
            item.email.last_error = f"Reviewed, but Gmail filed/read label failed: {exc}"

    def _mark_gmail_skipped(self, item: NeedsReview) -> None:
        if is_upload_message_id(item.email.gmail_message_id):
            return
        try:
            self.gmail.mark_skipped(item.email.gmail_message_id)
        except Exception as exc:
            item.email.last_error = f"Rejected, but Gmail skipped/read label failed: {exc}"

    def _store_final_metadata(self, item: NeedsReview, decision: ClassificationResult, audit: dict) -> None:
        item.metadata_json = {
            **(item.metadata_json or {}),
            "final": {
                "entity": decision.entity,
                "level2": decision.level2,
                "level3": decision.level3,
                "file_summary": decision.file_summary,
                "document_date": decision.document_date,
            },
            "decision_audit": audit,
        }

    def _store_negative_signal(self, item: NeedsReview) -> None:
        proposed = {
            "entity": item.proposed_entity,
            "level2": item.proposed_level2,
            "level3": item.proposed_level3,
            "file_summary": item.proposed_file_summary,
        }
        corrected = {
            "entity": item.corrected_entity,
            "level2": item.corrected_level2,
            "level3": item.corrected_level3,
            "file_summary": item.corrected_file_summary,
        }
        if proposed != corrected:
            item.metadata_json = {
                **(item.metadata_json or {}),
                "negative_signal": {
                    "proposed": proposed,
                    "corrected": corrected,
                    "reason": "Reviewer corrected Claude proposal.",
                },
            }

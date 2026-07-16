import re
from email.utils import parseaddr
from pathlib import Path

from sqlalchemy.orm import Session

from sqlalchemy import select

from app.db.models import FileArtifact, FilingLog, ProcessedEmail, ProcessedFile
from app.services.drive_service import DriveService
from app.services.rulebook_service import RulebookService
from app.services.types import ClassificationResult
from app.utils.files import resolve_artifact_path, safe_filename
from app.utils.time import date_prefix


def _normalize_confidence(value: object) -> float | None:
    # Mirror of the classifier/decision confidence normalization (accepts 0-1 or 0-100).
    if value is None:
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if 0 < confidence <= 1:
        return confidence * 100
    return confidence


def match_artifact_classification(audit: dict, artifact) -> dict | None:
    # The single source of truth for matching a (real) attachment to its per-attachment
    # classification entry. `artifact` only needs `.kind` and `.original_filename`, so this
    # works for both persisted FileArtifact rows and prepared artifacts. Returns the matched
    # dict, or None when no key matches (a miss must be *visible* so callers never silently
    # fall back to the email-level entity for auto-filing decisions).
    classifications = (audit or {}).get("artifact_classifications") or {}
    if not isinstance(classifications, dict):
        return None
    original = getattr(artifact, "original_filename", None)
    for key in [
        getattr(artifact, "kind", None),
        original,
        Path(original).name if original else None,
    ]:
        if key and isinstance(classifications.get(key), dict):
            return classifications[key]
    return None


def resolve_artifact_entity(audit: dict, artifact) -> tuple[str | None, float | None, bool]:
    # Resolve which entity a single attachment belongs to, from the per-attachment
    # classification. Returns (entity, entity_confidence, matched). `matched` is False when no
    # classification entry was found for this artifact, so the multi-entity gate can refuse to
    # auto-split rather than guess. Used by BOTH decision_service (the gate) and filing_service
    # (the actual routing) so the two can never disagree.
    matched = match_artifact_classification(audit, artifact)
    if matched is None:
        return None, None, False
    entity = (matched.get("entity") or "").strip() or None
    return entity, _normalize_confidence(matched.get("entity_confidence")), True


def is_decorative_artifact(artifact) -> bool:
    # True only when BOTH independent signals agreed (see apply_decorative_flags): the Gmail
    # part-classifier flagged the image as probably-decorative AND Claude, having read it,
    # confirmed. Decorative artifacts are skipped by the multi-entity confidence gate, excluded
    # from split assignments, and marked internal instead of filed standalone -- their content is
    # still preserved inside the combined email PDF archived to Communications.
    return bool((getattr(artifact, "metadata_json", None) or {}).get("decorative"))


def apply_decorative_flags(audit: dict, artifacts: list) -> None:
    # Stamp `decorative: true` onto artifacts where the part-classifier's suspicion
    # (metadata ambiguous_image, set at prep time) is CONFIRMED by Claude's per-attachment
    # `decorative` verdict. Requiring both signals means Claude alone can never suppress an
    # attachment the heuristics considered a real document, and a heuristic false-positive
    # (e.g. an iPhone screenshot of a lease) stays on the normal document path when Claude
    # recognizes it as real content. Stamped on the persisted FileArtifact row so the decision
    # gate, filing, review-approve, and the split UI all read the same verdict.
    classifications = (audit or {}).get("artifact_classifications") or {}
    if not isinstance(classifications, dict) or not classifications:
        return
    for artifact in artifacts:
        meta = getattr(artifact, "metadata_json", None) or {}
        if not meta.get("ambiguous_image") or meta.get("decorative"):
            continue
        matched = match_artifact_classification(audit, artifact)
        if matched and matched.get("decorative") is True:
            artifact.metadata_json = {**meta, "decorative": True}


class FilingService:
    def __init__(self) -> None:
        self.drive = DriveService()
        self.rulebook = RulebookService()

    def file_email_artifacts(
        self,
        db: Session,
        email: ProcessedEmail,
        classification: ClassificationResult,
        artifacts: list[FileArtifact],
    ) -> list[FileArtifact]:
        if not classification.entity or not classification.level2:
            raise ValueError("Cannot file without entity and Level 2 folder.")
        uploaded: list[FileArtifact] = []
        primary_path: str | None = None
        folder_path: str | None = None
        # Representative Drive path per entity, for one FilingLog per involved entity.
        entity_paths: dict[str, str] = {}
        drive_names = self.drive_filenames(email, classification, artifacts)
        for artifact in artifacts:
            if artifact.status == "unsupported":
                continue
            if not self._should_upload_artifact(artifact):
                self._mark_internal(artifact)
                continue
            path = resolve_artifact_path(artifact.generated_pdf_path) or resolve_artifact_path(artifact.local_path)
            if not path or not artifact.file_hash:
                continue
            # Dual filing: the combined email package is archived to the entity's Communications
            # folder; each real attachment is filed to its own Level 2 (its content's category),
            # falling back to the validated email-level category.
            target_folder_id, folder_path = self._artifact_target(db, email, classification, artifact)
            if artifact.kind != "combined_package":
                art_entity, _, _ = resolve_artifact_entity(classification.decision_audit, artifact)
                art_entity = art_entity or classification.entity
                if art_entity:
                    entity_paths.setdefault(art_entity, folder_path)
                if primary_path is None:
                    primary_path = folder_path
            filename = drive_names[artifact.id] or self._filename(email, classification, artifact)
            if artifact.drive_file_id and artifact.drive_folder_id == target_folder_id:
                if not self.drive.file_is_available_in_folder(artifact.drive_file_id, target_folder_id):
                    self._clear_drive_reference(artifact)
                else:
                    processed_file = db.execute(
                        select(ProcessedFile).where(
                            ProcessedFile.file_hash == artifact.file_hash,
                            ProcessedFile.drive_folder_id == target_folder_id,
                        )
                    ).scalars().first()
                    current_name = processed_file.filename if processed_file else None
                    if current_name != filename:
                        renamed = self.drive.rename_file(artifact.drive_file_id, filename)
                        artifact.drive_link = renamed.get("webViewLink") or artifact.drive_link
                        if processed_file:
                            processed_file.filename = filename
                    artifact.status = "filed"
                    self._stamp_folder_path(artifact, folder_path)
                    uploaded.append(artifact)
                    continue
            if artifact.drive_file_id and artifact.drive_folder_id and artifact.drive_folder_id != target_folder_id:
                existing = self.drive.find_valid_processed_file(db, artifact.file_hash, target_folder_id)
                if existing:
                    item = self.drive.get_drive_item(existing.drive_file_id) or {}
                    if existing.filename != filename:
                        item = self.drive.rename_file(existing.drive_file_id, filename)
                        existing.filename = filename
                    artifact.drive_file_id = existing.drive_file_id
                    artifact.drive_link = item.get("webViewLink") or f"https://drive.google.com/file/d/{existing.drive_file_id}/view"
                    artifact.drive_folder_id = target_folder_id
                    artifact.status = "duplicate"
                    self._stamp_folder_path(artifact, folder_path)
                    uploaded.append(artifact)
                    continue
                if not self.drive.file_is_available_in_folder(artifact.drive_file_id, artifact.drive_folder_id):
                    self._clear_drive_reference(artifact)
                else:
                    moved = self.drive.move_file(artifact.drive_file_id, artifact.drive_folder_id, target_folder_id, filename)
                    processed_file = db.execute(
                        select(ProcessedFile).where(
                            ProcessedFile.file_hash == artifact.file_hash,
                            ProcessedFile.drive_folder_id == artifact.drive_folder_id,
                        )
                    ).scalars().first()
                    if processed_file:
                        processed_file.drive_folder_id = target_folder_id
                        processed_file.filename = filename
                    artifact.drive_link = moved.get("webViewLink")
                    artifact.drive_folder_id = target_folder_id
                    artifact.status = "filed"
                    self._stamp_folder_path(artifact, folder_path)
                    uploaded.append(artifact)
                    continue
            if artifact.drive_file_id and not artifact.drive_folder_id:
                if not self.drive.get_drive_item(artifact.drive_file_id):
                    self._clear_drive_reference(artifact)
            drive_file, duplicate = self.drive.upload_pdf_once(
                db=db,
                local_path=path,
                filename=filename,
                drive_folder_id=target_folder_id,
                file_hash=artifact.file_hash,
                source_email_id=email.id,
            )
            artifact.drive_file_id = drive_file["id"]
            artifact.drive_link = drive_file.get("webViewLink")
            artifact.drive_folder_id = target_folder_id
            artifact.status = "duplicate" if duplicate else "filed"
            self._stamp_folder_path(artifact, folder_path)
            uploaded.append(artifact)

        # Entities this email ACTUALLY filed into -- ONLY those with a real destination path in
        # entity_paths (i.e. a document routed to them), primary first. An entity Claude merely
        # *named* (e.g. a property mentioned in a wire memo) but that no attachment routed to must
        # NOT get a combined-PDF copy or a "filed" activity row -- that produced a stray copy in
        # the wrong client's Communications folder and a phantom activity row.
        involved_entities = self._involved_entities(classification, artifacts)
        filed_entities: list[str] = []
        for e in involved_entities:
            if e and e in entity_paths and e not in filed_entities:
                filed_entities.append(e)
        # Safety net: the primary entity is always considered filed (its combined PDF is archived
        # to its Communications in the loop above), even if only the combined package existed.
        primary = classification.entity
        if primary and primary not in filed_entities:
            filed_entities.insert(0, primary)

        # Multi-entity split: copy the combined email PDF into each ADDITIONAL truly-involved
        # entity's Communications so that client's folder carries the full email context.
        self._copy_combined_to_entities(db, email, classification, artifacts, drive_names, filed_entities[1:])

        # One FilingLog per truly-involved entity (index 0 = primary carries the canonical link).
        primary_link = uploaded[0].drive_link if uploaded else None
        for index, entity in enumerate(filed_entities or [classification.entity]):
            db.add(
                FilingLog(
                    email_id=email.id,
                    sender=email.sender,
                    subject=email.subject,
                    entity=entity,
                    folder_path=entity_paths.get(entity) or primary_path or folder_path,
                    confidence=classification.confidence,
                    drive_link=primary_link if index == 0 else None,
                    status="filed",
                    message=classification.reason,
                )
            )
        db.commit()
        return uploaded

    def drive_filenames(
        self,
        email: ProcessedEmail,
        classification: ClassificationResult,
        artifacts: list[FileArtifact],
    ) -> dict[int, str | None]:
        # Drive names for all of an email's artifacts, keyed by artifact id. None for
        # artifacts that are never uploaded (e.g. the standalone email-body PDF).
        # When the AI gave no per-attachment summary, several attachments fall back to
        # the same email-level summary; suffix collisions with the original filename
        # (or a counter) so every file stays identifiable in Drive.
        names: dict[int, str | None] = {}
        for artifact in artifacts:
            if artifact.status == "unsupported" or not self._should_upload_artifact(artifact):
                names[artifact.id] = None
            else:
                names[artifact.id] = self._filename(email, classification, artifact)
        used: set[str] = set()
        for artifact in artifacts:
            name = names[artifact.id]
            if not name:
                continue
            if name not in used:
                used.add(name)
                continue
            base = name[:-4] if name.lower().endswith(".pdf") else name
            stem = Path(artifact.original_filename).stem if artifact.original_filename else ""
            candidate = safe_filename(f"{base} - {stem}.pdf") if stem else ""
            if not candidate or candidate in used:
                counter = 2
                while (candidate := safe_filename(f"{base} ({counter}).pdf")) in used:
                    counter += 1
            used.add(candidate)
            names[artifact.id] = candidate
        return names

    def _involved_entities(self, classification: ClassificationResult, artifacts: list[FileArtifact]) -> list[str]:
        # Distinct entities this email files into: the primary (email-level) entity first, then
        # any additional per-attachment entities (multi-entity split). Index 0 is the primary,
        # whose combined-PDF copy is the canonical Drive reference. For single-entity / legacy /
        # review-approve emails (no per-attachment entities in the audit) this is just [primary].
        ordered: list[str] = []
        seen: set[str] = set()
        candidates = [classification.entity]
        for artifact in artifacts:
            if artifact.kind in {"combined_package", "email_body"}:
                continue
            if artifact.status == "unsupported" or not self._should_upload_artifact(artifact):
                continue
            entity, _, _ = resolve_artifact_entity(classification.decision_audit, artifact)
            candidates.append(entity)
        for entity in candidates:
            key = (entity or "").strip().lower()
            if entity and key not in seen:
                seen.add(key)
                ordered.append(entity)
        return ordered

    def _copy_combined_to_entities(
        self,
        db: Session,
        email: ProcessedEmail,
        classification: ClassificationResult,
        artifacts: list[FileArtifact],
        drive_names: dict[int, str | None],
        entities: list[str],
    ) -> None:
        # Upload a copy of the combined email PDF into each given entity's Communications folder.
        # Idempotent via upload_pdf_once (dedup by file_hash + folder), so retries never
        # duplicate. Used for the ADDITIONAL entities only; the primary copy is filed in the
        # main loop.
        if not entities:
            return
        combined = next((a for a in artifacts if a.kind == "combined_package"), None)
        if not combined or not self._should_upload_artifact(combined) or not combined.file_hash:
            return
        path = resolve_artifact_path(combined.generated_pdf_path) or resolve_artifact_path(combined.local_path)
        if not path:
            return
        filename = drive_names.get(combined.id) or self._filename(email, classification, combined)
        for entity in entities:
            folder_id, _ = self.drive.resolve_target_folder(db, entity, "Communications", None)
            self.drive.upload_pdf_once(
                db=db,
                local_path=path,
                filename=filename,
                drive_folder_id=folder_id,
                file_hash=combined.file_hash,
                source_email_id=email.id,
            )

    def _stamp_folder_path(self, artifact: FileArtifact, folder_path: str | None) -> None:
        # Human-readable destination per artifact (the email-level FilingLog path can
        # differ under dual filing). Reassigned dict: JSON columns don't track mutation.
        if folder_path:
            artifact.metadata_json = {**(artifact.metadata_json or {}), "folder_path": folder_path}

    def _clear_drive_reference(self, artifact: FileArtifact) -> None:
        artifact.drive_file_id = None
        artifact.drive_link = None
        artifact.drive_folder_id = None
        if artifact.status in {"filed", "duplicate", "needs_review", "review_duplicate"}:
            artifact.status = "prepared"

    def _should_upload_artifact(self, artifact: FileArtifact) -> bool:
        # Client-facing filing output is one email package PDF (email + attachment
        # contents) plus each real attachment PDF saved individually. The standalone
        # email-body PDF is only the first section used to build the package.
        if artifact.kind == "email_body":
            return False
        if artifact.kind == "combined_package":
            return self.drive.settings.upload_combined_package
        # Agreed-decorative signature/logo images are never filed as standalone documents --
        # their content already lives inside the combined email PDF in Communications. This is
        # the single choke point, so auto-file, review Approve, and split all behave the same.
        if is_decorative_artifact(artifact):
            return False
        return True

    def _mark_internal(self, artifact: FileArtifact) -> None:
        artifact.drive_file_id = None
        artifact.drive_link = None
        artifact.drive_folder_id = None
        artifact.status = "internal"

    def file_to_needs_review_folder(self, db: Session, email: ProcessedEmail, artifacts: list[FileArtifact]) -> None:
        folder = self.drive.ensure_needs_review_folder()
        for artifact in artifacts:
            if artifact.status == "unsupported":
                continue
            if not self._should_upload_artifact(artifact):
                self._mark_internal(artifact)
                continue
            path = resolve_artifact_path(artifact.generated_pdf_path) or resolve_artifact_path(artifact.local_path)
            if not path or not artifact.file_hash:
                continue
            base = Path(artifact.original_filename).stem if artifact.original_filename else artifact.kind
            filename = safe_filename(f"{date_prefix(email.received_at)} - Review - {base}.pdf")
            drive_file, duplicate = self.drive.upload_pdf_once(db, path, filename, folder["id"], artifact.file_hash, email.id)
            artifact.drive_file_id = drive_file["id"]
            artifact.drive_link = drive_file.get("webViewLink")
            artifact.drive_folder_id = folder["id"]
            artifact.status = "review_duplicate" if duplicate else "needs_review"
        db.commit()

    def _artifact_target(
        self,
        db: Session,
        email: ProcessedEmail,
        classification: ClassificationResult,
        artifact: FileArtifact,
    ) -> tuple[str, str]:
        # The combined email package is archived to Communications; attachments go to their own
        # category. Returns (drive_folder_id, human-readable folder path).
        if artifact.kind == "combined_package":
            return self.drive.resolve_target_folder(db, classification.entity, "Communications", None)
        # Per-attachment entity (multi-entity split): each attachment may belong to a different
        # client. Falls back to the email-level entity for single-entity / legacy emails.
        artifact_entity, _, _ = resolve_artifact_entity(classification.decision_audit, artifact)
        artifact_entity = artifact_entity or classification.entity
        level2, level3 = self._attachment_target_spec(classification, artifact)
        return self.drive.resolve_target_folder(db, artifact_entity, level2, level3)

    def _attachment_target_spec(self, classification: ClassificationResult, artifact: FileArtifact) -> tuple[str, str | None]:
        # Per-attachment Level 2/3 from Claude, validated against the rulebook. Falls back to the
        # validated email-level category when the attachment's category is missing or invalid, so
        # the target is always a real folder. Never files an attachment to Communications.
        allowed = self.rulebook.allowed_level2()
        per = self._artifact_classification(classification, artifact)
        level2 = per.get("level2")
        if level2 not in allowed or level2 == "Communications":
            level2 = classification.level2
        level3 = self.rulebook.normalize_level3(level2, per.get("level3"))
        rule = self.rulebook.subfolder_rule_for(level2)
        if rule == "by_year" and not level3:
            level3 = self._document_year(classification)
        elif rule not in {"none", "by_year"} and not level3:
            # Required Level 3 missing for this attachment -> use the validated email-level target.
            level2, level3 = classification.level2, classification.level3
        return level2, level3

    def _artifact_classification(self, classification: ClassificationResult, artifact: FileArtifact) -> dict:
        return match_artifact_classification(classification.decision_audit, artifact) or {}

    def _document_year(self, classification: ClassificationResult) -> str:
        stamp = classification.decision_audit.get("filename_date") or (classification.document_date or "")
        digits = re.findall(r"\d{4}", stamp)
        return digits[0] if digits else date_prefix(None)[:4]

    def _filename(self, email: ProcessedEmail, classification: ClassificationResult, artifact: FileArtifact) -> str:
        prefix = classification.decision_audit.get("filename_date") or date_prefix(email.received_at)
        if artifact.kind == "combined_package":
            return self._communications_filename(email, classification, prefix)
        summary = safe_filename(self._trim_redundant_date(self._summary_for_artifact(classification, artifact)))
        if artifact.kind == "email_body":
            return f"{prefix} - Email Regarding {summary}.pdf"
        return f"{prefix} - {summary}.pdf"

    def _trim_redundant_date(self, summary: str) -> str:
        # The date prefix already carries the date — drop a month/year that slipped into the
        # summary (e.g. "... May 2026 Owner Report" -> "... Owner Report"). Safe: only removes
        # date-like tokens, never the document description.
        months = (
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
            r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        )
        value = re.sub(rf"\b{months}\s+(?:19|20)\d{{2}}\b", "", summary or "", flags=re.I)
        # Strip a bare year only when it appears at the END of the summary (trailing year after
        # the document description) or preceded by a space/dash (not a leading address number).
        # This avoids stripping "2002" in "2002 Frankford Owner Report" while still removing
        # a trailing "2026" in "Owner Report 2026".
        value = re.sub(r"(?<!\d)(?:19|20)\d{2}\b(?!\s*\w)", "", value)
        value = re.sub(r"\s{2,}", " ", value).strip(" -,")
        return value or (summary or "")

    def _communications_filename(self, email: ProcessedEmail, classification: ClassificationResult, prefix: str) -> str:
        # Client convention for the archived email: "<date> - <Original Sender> - <Subject>.pdf".
        sender = classification.decision_audit.get("email_sender") or self._sender_display(email.sender)
        subject = self._clean_subject(email.subject)
        parts = [prefix, *([sender] if sender else []), *([subject] if subject else [])]
        return safe_filename(" - ".join(parts) + ".pdf")

    def _sender_display(self, raw: str | None) -> str:
        name, addr = parseaddr(raw or "")
        return (name or addr or "").strip()

    def _clean_subject(self, value: str | None) -> str:
        value = value or ""
        while True:
            updated = re.sub(r"^\s*(fwd?|re):\s*", "", value, flags=re.I).strip()
            if updated == value.strip():
                return updated
            value = updated

    def _summary_for_artifact(self, classification: ClassificationResult, artifact: FileArtifact) -> str:
        summaries = classification.decision_audit.get("artifact_summaries") or {}
        if not isinstance(summaries, dict):
            summaries = {}
        keys = [
            artifact.kind,
            artifact.original_filename,
            Path(artifact.original_filename).name if artifact.original_filename else None,
        ]
        for key in keys:
            if key and summaries.get(key):
                return str(summaries[key])
        return classification.file_summary

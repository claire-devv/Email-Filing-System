from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr

from app.core.config import get_settings
from app.db.models import Entity, ProcessedEmail
from app.services.filing_service import is_decorative_artifact, resolve_artifact_entity
from app.services.rulebook_service import RulebookService
from app.services.types import ClassificationResult, PreparedEmail


ALLOWED_ACTIONS = {"file", "needs_review", "reject"}
# Reasons that a human's explicit Approve/Correct (force_file=True) is allowed to OVERRIDE. These
# are quality/soft warnings the reviewer has already seen and accepted by clicking Approve. Only
# STRUCTURAL problems (unknown entity, missing/invalid folder) still block a human action, since
# those would produce a genuinely broken filing path.
HUMAN_OVERRIDABLE_REASONS = {
    "partial_conversion_failure",
    "conversion_failure",
    "low_confidence",
    "multiple_entities",
    "too_many_entities",
    "claude_requested_review",
    "upload_entity_mismatch",
    "unsafe_reject",
}
# Level-2 folders that exist as upload SOURCES (clients/team drop files in) but must NEVER be a
# filing DESTINATION -- filing a document INTO an uploads folder would leave it "unfiled". Like
# "Communications"/"Needs Review" are special-cased elsewhere, a doc classified here is invalid.
UPLOAD_SOURCE_FOLDERS = {"client uploads", "rres uploads"}
REJECT_TERMS = {
    "ad",
    "advertisement",
    "marketing",
    "newsletter",
    "notification",
    "promo",
    "promotion",
    "social",
    "spam",
    "unsubscribe",
    "unrelated",
    "instagram",
    "facebook",
    "linkedin",
}
STOPWORDS = {
    "about",
    "attached",
    "email",
    "from",
    "fwd",
    "receipt",
    "subject",
    "that",
    "this",
    "with",
    # Common real-estate subject words that identify a document type, not a specific entity.
    # Storing these as keyword mappings creates cross-entity contamination (any future lease,
    # invoice, or amortization email from a different client would get the wrong entity hint).
    "amortization",
    "executed",
    "invoice",
    "lease",
    "please",
    "update",
}


@dataclass
class DecisionValidation:
    decision: ClassificationResult
    final_action: str
    reasons: list[str]
    audit: dict

    @property
    def should_file(self) -> bool:
        return self.final_action == "file"

    @property
    def should_review(self) -> bool:
        return self.final_action == "needs_review"

    @property
    def should_reject(self) -> bool:
        return self.final_action == "reject"


class DecisionValidator:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.rulebook = RulebookService()

    def validate(
        self,
        decision: ClassificationResult,
        email: ProcessedEmail,
        issues: list[str],
        entities: list[Entity],
        *,
        allow_new_entity: bool = False,
        force_file: bool = False,
        artifacts: list | None = None,
    ) -> DecisionValidation:
        reasons: list[str] = []
        audit: dict = {
            **(decision.decision_audit or {}),
            "requested_action": decision.action,
            "level3_before": decision.level3,
            "issues": issues,
            "failed_attachment_count": self._failed_attachment_count(issues),
        }

        action = (decision.action or "").strip().lower()
        if action not in ALLOWED_ACTIONS:
            reasons.append("invalid_action")
            action = "needs_review"

        decision.confidence = self._normalize_confidence(decision.confidence)
        audit["normalized_confidence"] = decision.confidence

        decision.unknown_entity = self._parse_bool(decision.unknown_entity, default=False)
        decision.needs_review = self._parse_bool(
            decision.needs_review,
            default=action == "needs_review",
        )

        filename_date, date_audit = self._filename_date(decision.document_date, email.received_at)
        audit.update(date_audit)
        audit["filename_date"] = filename_date.strftime("%Y.%m.%d")
        decision.document_date = filename_date.isoformat()
        clear_reject = action == "reject" and self._clear_reject(decision, email)

        if not clear_reject and decision.level2 not in self.rulebook.allowed_level2():
            reasons.append("invalid_level2")
        elif not clear_reject and (decision.level2 or "").strip().lower() in UPLOAD_SOURCE_FOLDERS:
            # An uploads folder is a SOURCE, never a filing destination -> route to human review.
            reasons.append("invalid_level2")

        normalized_level3 = self.rulebook.normalize_level3(decision.level2, decision.level3)
        decision.level3 = normalized_level3
        audit["level3_after"] = decision.level3

        subfolder_rule = self.rulebook.subfolder_rule_for(decision.level2)
        if not clear_reject and subfolder_rule == "by_year" and not decision.level3:
            decision.level3 = str(filename_date.year)
            audit["level3_after"] = decision.level3
        elif not clear_reject and subfolder_rule not in {"none", "by_year"} and not decision.level3:
            reasons.append("missing_required_level3")

        known_entities = {entity.entity_name for entity in entities}
        if clear_reject:
            decision.unknown_entity = False
        elif not decision.entity:
            decision.unknown_entity = True
            reasons.append("missing_entity")
        elif decision.entity not in known_entities:
            if allow_new_entity and self._valid_new_entity_name(decision.entity):
                audit["new_entity_requested"] = True
                decision.unknown_entity = False
            else:
                decision.unknown_entity = True
                reasons.append("unknown_entity")
        else:
            decision.unknown_entity = False

        # "ambiguous_image_part" = an inline Outlook/photo image we KEPT as a possible attachment
        # out of caution. NOTHING failed to read, so it must never surface as a conversion
        # failure ("an attachment couldn't be fully read") -- that misled reviewers whenever an
        # email was held for an unrelated reason. If the image is a real document (Claude says
        # decorative:false), the multi-entity gate below already requires a confident Known
        # entity for it like any other attachment; if it's decoration (both signals agree), the
        # gate skips it entirely. Either way the issue string carries no extra signal.
        real_issues = [item for item in issues if "ambiguous_image_part" not in item.lower()]
        if real_issues:
            if any("email body" in item.lower() and "conversion" in item.lower() for item in real_issues):
                reasons.append("conversion_failure")
            else:
                reasons.append("partial_conversion_failure")

        if action == "reject":
            if clear_reject:
                final_action = "reject"
            else:
                final_action = "needs_review"
                reasons.append("unsafe_reject")
        elif force_file:
            # A human explicitly Approved/Corrected -> override quality/soft warnings they've
            # already seen; only genuinely STRUCTURAL reasons still block.
            blocking = [r for r in reasons if r not in HUMAN_OVERRIDABLE_REASONS]
            final_action = "file" if not blocking else "needs_review"
        elif action == "needs_review" or decision.needs_review:
            final_action = "needs_review"
            if action == "needs_review":
                reasons.append("claude_requested_review")
        else:
            final_action = "file"

        # Always compute the multi-entity plan so auto_split_entities is written to the audit
        # even when Claude returned needs_review directly. The frontend split banner reads this
        # field -- without it, a multi-entity email shows no split option to the reviewer.
        additional = audit.get("additional_entities") or []
        distinct, every_confident_known, all_matched = self._multi_entity_plan(
            audit, decision.entity, artifacts, known_entities
        )
        is_multi = bool(additional) or len(distinct) > 1
        if is_multi:
            audit["auto_split_entities"] = sorted(distinct)

        if final_action == "file":
            # The auto-file confidence gate guards *unattended* filing only. A human
            # Approve/Correct (force_file=True) is an explicit override of that gate.
            # Structural reasons below (unknown entity, bad Level 2/3, conversion
            # failures) still block filing even for a human action.
            # Multi-entity email: each attachment may belong to a different client. The system
            # can auto-split (file each attachment to its own client, copy the email PDF to each)
            # ONLY when it is confident about every attachment; otherwise a human routes it.
            if not force_file:
                if is_multi:
                    required = set(additional) | ({decision.entity} if decision.entity else set())
                    if len(distinct) > self.settings.max_auto_split_entities:
                        reasons.append("too_many_entities")
                    elif not (all_matched and every_confident_known):
                        # Some attachment couldn't be matched to a confident, known entity.
                        reasons.append("multiple_entities")
                    elif not distinct.issuperset(required):
                        # Per-attachment routing doesn't cover an entity Claude flagged -> can't
                        # safely split, so a human decides.
                        reasons.append("multiple_entities")
            if not force_file and decision.confidence < self.settings.auto_file_confidence:
                final_action = "needs_review"
                reasons.append("low_confidence")
            if decision.unknown_entity:
                final_action = "needs_review"
                reasons.append("unknown_entity")
            # Any remaining reason blocks an unattended file. For a human Approve/Correct
            # (force_file), only STRUCTURAL reasons block -- quality/soft warnings they've already
            # seen are overridden (see HUMAN_OVERRIDABLE_REASONS).
            blocking = [r for r in reasons if not force_file or r not in HUMAN_OVERRIDABLE_REASONS]
            if blocking:
                final_action = "needs_review"

        if final_action == "needs_review" and decision.confidence < self.settings.urgent_review_confidence:
            decision.urgent = True
        decision.needs_review = final_action == "needs_review"
        decision.action = final_action
        decision.needs_review_reason = decision.needs_review_reason or ", ".join(dict.fromkeys(reasons)) or None
        audit["action"] = final_action
        audit["reasons"] = list(dict.fromkeys(reasons))
        decision.decision_audit = audit
        return DecisionValidation(decision=decision, final_action=final_action, reasons=audit["reasons"], audit=audit)

    def _multi_entity_plan(
        self,
        audit: dict,
        primary_entity: str | None,
        artifacts: list | None,
        known_entities: set[str],
    ) -> tuple[set[str], bool, bool]:
        # Resolve every real attachment to its entity using the SAME matching filing uses, so the
        # gate and filing can never disagree. Returns:
        #   distinct                 -- the set of entities this email would file into (incl. primary)
        #   every_confident_known    -- every attachment matched a Known entity at >= auto-file conf
        #   all_matched              -- every attachment had a per-attachment classification entry
        threshold = self.settings.auto_file_confidence
        distinct: set[str] = set()
        if primary_entity:
            distinct.add(primary_entity)
        every_confident_known = True
        all_matched = True
        for artifact in artifacts or []:
            if getattr(artifact, "kind", None) in {"combined_package", "email_body"}:
                continue
            if getattr(artifact, "status", None) == "unsupported":
                continue
            # Agreed-decorative signature/logo images carry no entity signal and are never filed
            # standalone -- they must not force "every attachment confidently matched" to fail
            # (a logo blocking two perfectly confident loan statements from auto-splitting).
            if is_decorative_artifact(artifact):
                continue
            entity, confidence, matched = resolve_artifact_entity(audit, artifact)
            if not matched:
                # A keying miss would silently fall back to the primary entity at filing time;
                # treat it as not-auto-fileable so we never misroute an attachment.
                all_matched = False
                every_confident_known = False
                continue
            if entity:
                distinct.add(entity)
            if (not entity) or (entity not in known_entities) or (confidence is None) or (confidence < threshold):
                every_confident_known = False
        return distinct, every_confident_known, all_matched

    def from_review_values(
        self,
        *,
        entity: str | None,
        level2: str | None,
        level3: str | None,
        file_summary: str | None,
        document_date: str | None,
        reason: str,
        confidence: float = 100,
    ) -> ClassificationResult:
        return ClassificationResult(
            entity=entity,
            level2=level2,
            level3=level3,
            file_summary=file_summary or "Approved Filing Document",
            confidence=confidence,
            unknown_entity=False,
            needs_review=False,
            reason=reason,
            action="file",
            document_date=document_date,
        )

    def _normalize_confidence(self, value: object) -> float:
        confidence = float(value or 0)
        if 0 < confidence <= 1:
            return confidence * 100
        return confidence

    def _parse_bool(self, value: object, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1"}:
                return True
            if normalized in {"false", "no", "0"}:
                return False
        return default

    def _filename_date(self, raw_document_date: str | None, received_at: datetime | None) -> tuple[date, dict]:
        fallback = (received_at or datetime.now(timezone.utc)).date()
        audit = {"document_date_raw": raw_document_date, "document_date_source": "email_received"}
        if not raw_document_date:
            audit["document_date_rejected_reason"] = "missing"
            return fallback, audit
        try:
            parsed = datetime.fromisoformat(raw_document_date.replace("Z", "+00:00")).date()
        except ValueError:
            audit["document_date_rejected_reason"] = "malformed"
            return fallback, audit
        today = datetime.now(timezone.utc).date()
        if parsed.year < 2000:
            audit["document_date_rejected_reason"] = "before_2000"
            return fallback, audit
        if parsed > today + timedelta(days=30):
            audit["document_date_rejected_reason"] = "future_gt_30_days"
            return fallback, audit
        audit["document_date_source"] = "document"
        audit["document_date_rejected_reason"] = None
        return parsed, audit

    def _valid_new_entity_name(self, value: str) -> bool:
        # Human Correct is allowed to create the exact Drive Level 1 folder name the
        # reviewer chooses. Auto-filing still cannot create unknown entities because
        # allow_new_entity is false outside review/admin flows.
        value = value.strip()
        if not value or len(value) > 512:
            return False
        if any(char in value for char in ['\\', '/', ':', '*', '?', '"', '<', '>', '|']):
            return False
        return bool(re.search(r"[A-Za-z0-9]", value))

    def _clear_reject(self, decision: ClassificationResult, email: ProcessedEmail) -> bool:
        text = " ".join(
            [
                decision.reason or "",
                decision.needs_review_reason or "",
                decision.file_summary or "",
                email.subject or "",
                email.sender or "",
            ]
        ).lower()
        return any(term in text for term in REJECT_TERMS)

    def _failed_attachment_count(self, issues: list[str]) -> int:
        # ambiguous_image_part is a "kept out of caution" marker, not a failed attachment.
        return len([
            item for item in issues
            if "email body" not in item.lower() and "ambiguous_image_part" not in item.lower()
        ])


def subject_keywords(subject: str | None, limit: int = 5) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.-]{3,}", subject or "")
    output: list[str] = []
    for word in words:
        value = word.lower().strip(".-")
        if value in STOPWORDS or value in output:
            continue
        output.append(value)
        if len(output) >= limit:
            break
    return output


def sender_domain(sender: str | None) -> str:
    email = parseaddr(sender or "")[1].lower()
    return email.split("@", 1)[1] if "@" in email else ""

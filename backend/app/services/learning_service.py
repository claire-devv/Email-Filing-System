from collections import defaultdict
from email.utils import parseaddr

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import LearnedMapping
from app.services.decision_service import subject_keywords
from app.services.signals import (
    FREE_MAIL_DOMAINS,
    extract_signals,
    is_free_mail_domain,
)

# Base relevance weight per learned-mapping type, most -> least discriminating. A signal
# that maps to a single entity scores its full weight; one that maps to several entities
# (a shared vendor domain, a generic keyword) is demoted at lookup time (see top_relevant).
SIGNAL_WEIGHTS = {
    "address": 95,  # house number + street: near-unique per property
    "org": 90,      # LLC / LP name: explicit and unique
    "sender": 85,   # exact email address of a participant
    "domain": 60,   # organization domain
    "keyword": 40,  # weak subject-token fallback
}
# How much an ambiguous signal (one mapping to 2+ distinct entities) is penalized.
_AMBIGUITY_PENALTY = 60
# New typed signals use "email"; we persist them under the existing "sender" pattern_type
# so legacy sender mappings and the new email signals share one representation.
_SIGNAL_TYPE_TO_PATTERN = {"email": "sender"}


def is_forwarder_domain(domain: str | None) -> bool:
    # Internal relay inboxes (e.g. the RRES filing address) forward mail for every
    # client, so a learned sender/domain mapping for them would wrongly steer all
    # future forwarded email toward one entity.
    normalized = (domain or "").strip().lower()
    return normalized in {item.strip().lower() for item in get_settings().forwarder_domains}


class LearningService:
    def top_relevant(
        self,
        db: Session,
        sender: str | None,
        subject: str | None,
        limit: int = 10,
        *,
        signals: list[dict] | None = None,
        body_text: str | None = None,
    ) -> list[LearnedMapping]:
        # Reduce the email to typed signals (address/org/email/domain/keyword). Callers that
        # already computed signals (the classifier) pass them in; legacy callers pass only
        # sender/subject and we derive a lighter signal set here so they still benefit.
        if signals is None:
            forwarder_domains = {d.strip().lower() for d in get_settings().forwarder_domains or []}
            signals = extract_signals(
                sender=sender, subject=subject, body_text=body_text, forwarder_domains=forwarder_domains
            )

        # Group the lookup values by the pattern_type they are stored under, so we can fetch
        # every possibly-matching row in one indexed query regardless of table size.
        values_by_type: dict[str, set[str]] = defaultdict(set)
        for sig in signals:
            pattern_type = _SIGNAL_TYPE_TO_PATTERN.get(sig["type"], sig["type"])
            value = (sig.get("value") or "").strip().lower()
            if value:
                values_by_type[pattern_type].add(value)
        conditions = [
            and_(LearnedMapping.pattern_type == ptype, LearnedMapping.pattern_value.in_(values))
            for ptype, values in values_by_type.items()
            if values
        ]
        if not conditions:
            return []
        mappings = db.execute(
            select(LearnedMapping).where(LearnedMapping.active.is_(True), or_(*conditions))
        ).scalars().all()

        # Ambiguity demotion: a value that maps to several distinct entities is a shared
        # signal (vendor domain, generic keyword), not an identifying one. Count distinct
        # entities per matched value from the fetched rows and penalize the multi-entity ones.
        entities_per_value: dict[tuple[str, str], set] = defaultdict(set)
        for mapping in mappings:
            entities_per_value[(mapping.pattern_type, mapping.pattern_value.lower())].add(mapping.entity)

        def score(mapping: LearnedMapping) -> int:
            base = SIGNAL_WEIGHTS.get(mapping.pattern_type, 0)
            value = mapping.pattern_value.lower()
            if mapping.pattern_type == "keyword" and value.isdigit():
                base = 60  # pure-numeric token (house/unit number) is far more discriminating
            distinct_entities = len(entities_per_value[(mapping.pattern_type, value)])
            if distinct_entities > 1:
                base = max(0, base - _AMBIGUITY_PENALTY)
            return base

        ranked = [(score(item), item) for item in mappings]
        return [
            item
            for value, item in sorted(
                ranked,
                key=lambda pair: (pair[0], pair[1].confirmation_count, pair[1].updated_at),
                reverse=True,
            )
            if value > 0
        ][:limit]

    def record_signals(
        self,
        db: Session,
        signals: list[dict] | None,
        entity: str | None,
        level2: str | None,
        level3: str | None,
        source: str,
        note: str | None = None,
    ) -> None:
        # Record one mapping per typed signal extracted from a reviewed email. This is the
        # generic learning path: every discriminating thing about the email (the owner's
        # address, the LLC name, each non-forwarder participant email/domain) is learned at
        # once, so a single correction generalizes far beyond an identical subject line.
        for sig in signals or []:
            stype = sig.get("type")
            value = (sig.get("value") or "").strip().lower()
            if not stype or not value:
                continue
            pattern_type = _SIGNAL_TYPE_TO_PATTERN.get(stype, stype)
            if pattern_type == "domain" and (is_free_mail_domain(value) or is_forwarder_domain(value)):
                continue  # never learn a shared free-mail or relay domain
            self._upsert(db, pattern_type, value, entity, level2, level3, source, note)

    def record_sender_mapping(
        self,
        db: Session,
        sender: str | None,
        entity: str | None,
        level2: str | None,
        level3: str | None,
        source: str,
        note: str | None = None,
    ) -> None:
        email = parseaddr(sender or "")[1].lower()
        if not email:
            return
        domain = email.split("@", 1)[1] if "@" in email else ""
        if is_forwarder_domain(domain):
            return
        self._upsert(db, "sender", email, entity, level2, level3, source, note)
        if domain and not is_free_mail_domain(domain):
            self._upsert(db, "domain", domain, entity, level2, level3, source, note)

    def record_review_mapping(
        self,
        db: Session,
        sender: str | None,
        subject: str | None,
        entity: str | None,
        level2: str | None,
        level3: str | None,
        source: str,
        note: str | None = None,
    ) -> None:
        self.record_sender_mapping(db, sender, entity, level2, level3, source, note)
        for keyword in subject_keywords(subject):
            self._upsert(db, "keyword", keyword, entity, level2, level3, source, note)

    def _upsert(
        self,
        db: Session,
        pattern_type: str,
        pattern_value: str,
        entity: str | None,
        level2: str | None,
        level3: str | None,
        source: str,
        note: str | None = None,
    ) -> None:
        existing = db.execute(
            select(LearnedMapping).where(
                LearnedMapping.pattern_type == pattern_type,
                LearnedMapping.pattern_value == pattern_value,
                LearnedMapping.entity == entity,
                LearnedMapping.level2 == level2,
            )
        ).scalars().first()
        if existing:
            existing.confirmation_count += 1
            existing.confidence_boost = min(20, existing.confidence_boost + 2)
            existing.level3 = level3
            existing.source = source
            if note:  # keep the latest non-empty reviewer rationale
                existing.note = note
            return
        db.add(
            LearnedMapping(
                pattern_type=pattern_type,
                pattern_value=pattern_value,
                entity=entity,
                level2=level2,
                level3=level3,
                confidence_boost=5,
                confirmation_count=1,
                source=source,
                note=note,
            )
        )

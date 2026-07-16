"""Phase 1 multi-entity filing — gate + parsing + resolver tests.

Run: python -m app.scripts.test_multi_entity_filing
"""
from datetime import datetime, timezone

from app.db.models import Entity, ProcessedEmail
from app.services.classifier_service import ClassifierService
from app.services.decision_service import DecisionValidator
from app.services.filing_service import resolve_artifact_entity
from app.services.types import ClassificationResult


class _Art:
    # Minimal stand-in for a FileArtifact: the gate/resolver only read kind, original_filename
    # and status.
    def __init__(self, kind, original_filename, status="prepared"):
        self.kind = kind
        self.original_filename = original_filename
        self.status = status


def _decision(audit, **kwargs) -> ClassificationResult:
    defaults = {
        "action": "file",
        "entity": "A LLC",
        "level2": "Property Taxes",
        "level3": None,
        "file_summary": "County Property Tax Receipt",
        "document_date": "2026-05-06",
        "confidence": 95,
        "unknown_entity": False,
        "needs_review": False,
        "reason": "test",
        "decision_audit": audit,
    }
    defaults.update(kwargs)
    return ClassificationResult(**defaults)


def _classif(entity, conf, level2="Property Taxes"):
    return {"summary": "Doc", "level2": level2, "level3": None, "entity": entity, "entity_confidence": conf}


def main() -> None:
    validator = DecisionValidator()
    email = ProcessedEmail(
        gmail_message_id="t", subject="reports", sender="c@example.com",
        received_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
    )
    # Enough known entities to exceed the configured auto-split cap by at least one.
    cap = validator.settings.max_auto_split_entities
    entity_names = [f"E{i} LLC" for i in range(cap + 2)]
    entities = [Entity(entity_name=n, folder_name=n) for n in ["A LLC", "B LLC", "C LLC", *entity_names]]

    def run(audit, artifacts, **kw):
        return validator.validate(_decision(audit, **kw), email, [], entities, artifacts=artifacts)

    # 1. Confident 2-entity split -> auto-file, no multi reason.
    split = run(
        {"artifact_classifications": {"a.pdf": _classif("A LLC", 95), "b.pdf": _classif("B LLC", 90)}},
        [_Art("attachment", "a.pdf"), _Art("attachment", "b.pdf")],
    )
    assert split.final_action == "file", split.reasons
    assert "multiple_entities" not in split.reasons and "too_many_entities" not in split.reasons
    assert split.audit["auto_split_entities"] == ["A LLC", "B LLC"], split.audit.get("auto_split_entities")

    # 2. One attachment below auto-file confidence -> review.
    low = run(
        {"artifact_classifications": {"a.pdf": _classif("A LLC", 95), "b.pdf": _classif("B LLC", 40)}},
        [_Art("attachment", "a.pdf"), _Art("attachment", "b.pdf")],
    )
    assert low.final_action == "needs_review" and "multiple_entities" in low.reasons, low.reasons

    # 3. An attachment with no matching classification entry (keying miss) -> review (no silent
    #    fallback to the primary entity).
    miss = run(
        {"artifact_classifications": {"a.pdf": _classif("A LLC", 95), "b.pdf": _classif("B LLC", 90)}},
        [_Art("attachment", "a.pdf"), _Art("attachment", "b.pdf"), _Art("attachment", "ghost.pdf")],
    )
    assert miss.final_action == "needs_review" and "multiple_entities" in miss.reasons, miss.reasons

    # 4. additional_entities flags an entity that no attachment routes to -> review.
    uncovered = run(
        {
            "artifact_classifications": {"a.pdf": _classif("A LLC", 95)},
            "additional_entities": ["C LLC"],
        },
        [_Art("attachment", "a.pdf")],
    )
    assert uncovered.final_action == "needs_review" and "multiple_entities" in uncovered.reasons, uncovered.reasons

    # 5. More than the configured cap of distinct entities -> too_many_entities.
    over = entity_names[: cap + 1]  # cap+1 distinct known entities -> exceeds the cap
    too_many = run(
        {"artifact_classifications": {f"{n}.pdf": _classif(n, 95) for n in over}},
        [_Art("attachment", f"{n}.pdf") for n in over],
    )
    assert too_many.final_action == "needs_review" and "too_many_entities" in too_many.reasons, too_many.reasons

    # 6. Single-entity regression: every attachment maps to the same (primary) entity.
    single = run(
        {"artifact_classifications": {"a.pdf": _classif("A LLC", 95), "b.pdf": _classif("A LLC", 95)}},
        [_Art("attachment", "a.pdf"), _Art("attachment", "b.pdf")],
    )
    assert single.final_action == "file", single.reasons
    assert "multiple_entities" not in single.reasons
    assert "auto_split_entities" not in single.audit  # not flagged multi

    # 7. _normalize_artifact_summaries captures entity + entity_confidence.
    _, classifications = ClassifierService()._normalize_artifact_summaries(
        {"a.pdf": {"summary": "X", "level2": "Insurance", "level3": None, "entity": "B LLC", "entity_confidence": 88}}
    )
    assert classifications["a.pdf"]["entity"] == "B LLC"
    assert classifications["a.pdf"]["entity_confidence"] == 88

    # 8. resolve_artifact_entity: match (0-1 conf normalized) + miss.
    audit = {"artifact_classifications": {"a.pdf": {"entity": "B LLC", "entity_confidence": 0.9}}}
    ent, conf, matched = resolve_artifact_entity(audit, _Art("attachment", "a.pdf"))
    assert ent == "B LLC" and matched is True and conf == 90.0, (ent, conf, matched)
    ent2, _, matched2 = resolve_artifact_entity(audit, _Art("attachment", "missing.pdf"))
    assert matched2 is False and ent2 is None

    print("multi-entity filing: all assertions passed")
    print({
        "split": split.reasons, "low": low.reasons, "miss": miss.reasons,
        "uncovered": uncovered.reasons, "too_many": too_many.reasons, "single": single.reasons,
    })


if __name__ == "__main__":
    main()

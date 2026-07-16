"""Phase 2 backend test: ReviewService.file_split.

Verifies per-attachment routing into a synthetic audit, the unassigned-attachment guard, and the
invalid-assignment guard -- without touching Drive (the filing layer is faked).

Run: python -m app.scripts.test_review_file_split
"""
import types

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.db.models import FileArtifact, NeedsReview, ProcessedEmail
from app.schemas.review import SplitAssignment
from app.services.review_service import ReviewService


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    email = ProcessedEmail(gmail_message_id="m1", sender="file@rockreservices.com", subject="Two reports", status="pending_review")
    db.add(email)
    db.flush()
    a1 = FileArtifact(email_id=email.id, kind="attachment", original_filename="1339_report.pdf", local_path="/x/1339.pdf", file_hash="h1", status="needs_review")
    a2 = FileArtifact(email_id=email.id, kind="attachment", original_filename="girard_report.pdf", local_path="/x/girard.pdf", file_hash="h2", status="needs_review")
    combined = FileArtifact(email_id=email.id, kind="combined_package", original_filename=None, local_path="/x/combined.pdf", file_hash="h3", status="needs_review")
    db.add_all([a1, a2, combined])
    db.flush()
    review = NeedsReview(email_id=email.id, status="pending", proposed_entity="J. Claffey - 1339-45 N Front",
                         proposed_level2="Client Reporting", proposed_file_summary="Reports", metadata_json={"decision_audit": {"email_sender": "Matthew Rodrigue"}})
    db.add(review)
    db.flush()
    return email, a1, a2, combined, review


def _service(known_entity_names):
    svc = ReviewService()
    captured = {}

    def fake_file(db, email, classification, artifacts):
        captured["classification"] = classification
        captured["artifacts"] = artifacts
        return artifacts

    svc.filing = types.SimpleNamespace(file_email_artifacts=fake_file)
    svc.gmail = types.SimpleNamespace(mark_filed=lambda *_a, **_k: None, mark_skipped=lambda *_a, **_k: None)
    svc.entities = types.SimpleNamespace(
        list_active=lambda db: [types.SimpleNamespace(entity_name=n) for n in known_entity_names],
        add_alias=lambda *a, **k: None,
    )
    return svc, captured


def main() -> None:
    E1 = "J. Claffey - 1339-45 N Front"
    E2 = "J. Claffey - 210 W. Girard Owner LLC"

    # --- Case 1: valid 2-entity split -> corrected, both entities routed. ---
    db = _session()
    email, a1, a2, combined, review = _seed(db)
    svc, captured = _service([E1, E2])
    assignments = [
        SplitAssignment(artifact_id=a1.id, entity=E1, level2="Client Reporting", level3="2026", file_summary="Cash Basis Financial Report"),
        SplitAssignment(artifact_id=a2.id, entity=E2, level2="Client Reporting", level3="2026", file_summary="Monthly Financial Reporting Package"),
    ]
    out = svc.file_split(db, review, assignments, document_date="2026-06-16", reviewed_by="tester")
    assert out.status == "corrected", out.status
    audit = captured["classification"].decision_audit
    ac = audit["artifact_classifications"]
    assert ac["1339_report.pdf"]["entity"] == E1, ac
    assert ac["girard_report.pdf"]["entity"] == E2, ac
    assert sorted(audit["auto_split_entities"]) == sorted([E1, E2]), audit["auto_split_entities"]
    assert audit["filename_date"] == "2026.06.16", audit["filename_date"]
    # The combined PDF is filed by file_email_artifacts (all artifacts passed through).
    assert any(a.kind == "combined_package" for a in captured["artifacts"])
    assert review.metadata_json.get("split") and len(review.metadata_json["split"]) == 2
    db.close()

    # --- Case 2: an attachment left unassigned -> rejected. ---
    db = _session()
    email, a1, a2, combined, review = _seed(db)
    svc, _ = _service([E1, E2])
    only_one = [SplitAssignment(artifact_id=a1.id, entity=E1, level2="Client Reporting", level3="2026", file_summary="Report")]
    try:
        svc.file_split(db, review, only_one, document_date="2026-06-16")
        raise AssertionError("expected ValueError for unassigned attachment")
    except ValueError as exc:
        assert "must be assigned" in str(exc), exc
    db.close()

    # --- Case 3: invalid level2 -> rejected. ---
    db = _session()
    email, a1, a2, combined, review = _seed(db)
    svc, _ = _service([E1, E2])
    bad = [
        SplitAssignment(artifact_id=a1.id, entity=E1, level2="Client Reporting", level3="2026", file_summary="R"),
        SplitAssignment(artifact_id=a2.id, entity=E2, level2="Not A Real Folder", level3=None, file_summary="R"),
    ]
    try:
        svc.file_split(db, review, bad, document_date="2026-06-16")
        raise AssertionError("expected ValueError for invalid level2")
    except ValueError as exc:
        assert "invalid" in str(exc).lower(), exc
    db.close()

    print("review file_split: all assertions passed")


if __name__ == "__main__":
    main()

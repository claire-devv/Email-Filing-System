"""Offline end-to-end test for the notification, review, and feedback-learning systems.

Runs with no real Google and an isolated temp SQLite DB:
- a dedicated temp engine backs all requests via a get_db dependency override,
- Drive filing is faked and Gmail label calls are no-ops,
- the rulebook loads from data/folder_structure.json at app import.

Run:
    .\\.venv\\Scripts\\python.exe -m app.scripts.test_review_learning_notifications
"""

import os
import tempfile
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db.models import Entity, FileArtifact, LearnedMapping, NeedsReview, ProcessedEmail
from app.db.session import Base, get_db
from app.services.entity_service import EntityService
from app.services.filing_service import FilingService
from app.services.gmail_service import GmailService
from app.services.learning_service import LearningService
from app.services.processing_service import ProcessingService

ENTITY = "J. Claffey - JSCD 761 S. Cleveland LLC"


def _fake_file_email_artifacts(self, db, email, classification, artifacts):
    # Stand-in for Drive filing: just mark the artifacts filed.
    for artifact in artifacts:
        artifact.status = "filed"
        db.add(artifact)
    return list(artifacts)


def _seed(TestSession) -> dict:
    db = TestSession()
    db.add(Entity(entity_name=ENTITY, folder_name=ENTITY, active=True))

    def add_email(message_id, sender, subject, received) -> ProcessedEmail:
        email = ProcessedEmail(
            gmail_message_id=message_id,
            sender=sender,
            subject=subject,
            received_at=received,
            status="pending_review",
            metadata_json={},
        )
        db.add(email)
        db.flush()
        return email

    def add_artifacts(email_id, kinds) -> None:
        for kind in kinds:
            db.add(
                FileArtifact(
                    email_id=email_id,
                    kind=kind,
                    original_filename=f"{kind}.pdf",
                    local_path=f"/tmp/{email_id}_{kind}.pdf",
                    generated_pdf_path=f"/tmp/{email_id}_{kind}.pdf",
                    file_hash=f"hash-{email_id}-{kind}",
                    size_bytes=10,
                    status="needs_review",
                    metadata_json={},
                )
            )

    # A: approvable, low confidence, free-mail sender.
    a = add_email("A", "Ata Ur Rehman <ataurrehman3636@gmail.com>", "Property tax receipt JSCD 761", datetime(2026, 5, 6, tzinfo=timezone.utc))
    review_a = NeedsReview(
        email_id=a.id, proposed_entity=ENTITY, proposed_level2="Property Taxes", proposed_level3=None,
        proposed_file_summary="San Diego County Property Tax Payment Receipt", claude_reasoning="receipt",
        confidence=78, urgent=False, status="pending",
        metadata_json={"issues": [], "proposed_document_date": "2026-05-06"},
    )
    db.add(review_a)
    add_artifacts(a.id, ["email_body", "attachment", "combined_package"])

    # B: urgent, to be rejected.
    b = add_email("B", "promo@gmail.com", "Newsletter promo", datetime(2026, 6, 1, tzinfo=timezone.utc))
    db.add(NeedsReview(email_id=b.id, proposed_entity=None, proposed_level2=None, confidence=20, urgent=True, status="pending", metadata_json={"issues": []}))
    add_artifacts(b.id, ["email_body"])

    # C: to be corrected (company-domain sender; proposed Level 2 differs from corrected).
    c = add_email("C", "Jason Hoffman <jhoffman@ginsgroup.com>", "Insurance policy JSCD 761", datetime(2026, 4, 29, tzinfo=timezone.utc))
    db.add(NeedsReview(
        email_id=c.id, proposed_entity=ENTITY, proposed_level2="Property Taxes", proposed_level3=None,
        proposed_file_summary="Misclassified guess", claude_reasoning="guess", confidence=55, urgent=False,
        status="pending", metadata_json={"issues": []},
    ))
    add_artifacts(c.id, ["email_body"])

    db.commit()
    ids = {
        "review_a": review_a.id, "email_a": a.id,
        "review_b": db.execute(select(NeedsReview.id).where(NeedsReview.email_id == b.id)).scalar(), "email_b": b.id,
        "review_c": db.execute(select(NeedsReview.id).where(NeedsReview.email_id == c.id)).scalar(), "email_c": c.id,
    }
    db.close()
    return ids


def main() -> None:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    test_engine = create_engine(f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False}, future=True)
    TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
    Base.metadata.create_all(bind=test_engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    original_file = FilingService.file_email_artifacts
    original_filed, original_skipped, original_failed = GmailService.mark_filed, GmailService.mark_skipped, GmailService.mark_failed
    FilingService.file_email_artifacts = _fake_file_email_artifacts
    GmailService.mark_filed = lambda self, message_id: None
    GmailService.mark_skipped = lambda self, message_id: None
    GmailService.mark_failed = lambda self, message_id: None

    try:
        ids = _seed(TestSession)
        client = TestClient(app)

        # 1. Notifications reflect seeded pending/urgent counts.
        counts = client.get("/notifications/counts").json()
        assert counts == {"pending_review_count": 3, "urgent_review_count": 1}, counts

        # 2. Review list returns all pending items, urgent first.
        items = client.get("/review/items", params={"status": "pending"}).json()
        assert len(items) == 3, items
        assert items[0]["urgent"] is True

        # 3. Reject the urgent item (offline-safe path).
        resp = client.post(f"/review/items/{ids['review_b']}/reject", json={"reason": "spam newsletter", "reviewed_by": "tester"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"
        with TestSession() as db:
            arts_b = db.execute(select(FileArtifact).where(FileArtifact.email_id == ids["email_b"])).scalars().all()
            assert arts_b and all(a.status == "rejected" for a in arts_b)
        counts = client.get("/notifications/counts").json()
        assert counts == {"pending_review_count": 2, "urgent_review_count": 0}, counts

        # 4. Approve the low-confidence item (human override; Drive faked).
        resp = client.post(f"/review/items/{ids['review_a']}/approve", json={"reviewed_by": "tester"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "approved"
        with TestSession() as db:
            arts_a = db.execute(select(FileArtifact).where(FileArtifact.email_id == ids["email_a"])).scalars().all()
            assert arts_a and all(a.status == "filed" for a in arts_a)
        assert client.get("/notifications/counts").json()["pending_review_count"] == 1

        # 5. Correct the misclassified item (new Level 2 -> negative signal recorded).
        resp = client.post(
            f"/review/items/{ids['review_c']}/correct",
            json={"entity": ENTITY, "level2": "Insurance", "level3": None,
                  "file_summary": "Gotham GL policy for JSCD 761", "document_date": "2026-04-29",
                  "reviewed_by": "tester", "alias": "Cleveland policy"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "corrected"
        assert body["corrected"]["level2"] == "Insurance"
        with TestSession() as db:
            review_c = db.get(NeedsReview, ids["review_c"])
            assert (review_c.metadata_json or {}).get("negative_signal") is not None
            # Alias learned on Correct is appended to the entity (and surfaced to Claude).
            entity = EntityService().get_by_name(db, ENTITY)
            assert "Cleveland policy" in (entity.aliases or [])
        assert client.get("/notifications/counts").json()["pending_review_count"] == 0

        # Entity import from Drive folders (folder names are the source of truth).
        with TestSession() as db:
            existing_aliases = list(EntityService().get_by_name(db, ENTITY).aliases or [])
            result = EntityService().import_entities(
                db,
                [
                    {"id": "drv-1", "name": "B. Dailey - Explorent LLC"},
                    {"id": "drv-2", "name": ENTITY},  # already present -> update, not duplicate
                ],
            )
            assert result["created"] == 1 and result["total"] == 2
            explorent = EntityService().get_by_name(db, "B. Dailey - Explorent LLC")
            assert explorent is not None and explorent.drive_folder_id == "drv-1"
            refreshed = EntityService().get_by_name(db, ENTITY)
            assert refreshed.drive_folder_id == "drv-2"
            refreshed_aliases = list(refreshed.aliases or [])
            # Import keeps learned aliases and merges in name-derived variants.
            assert all(alias in refreshed_aliases for alias in existing_aliases)
            assert "JSCD 761 S. Cleveland LLC" in refreshed_aliases

        # Master folders are the only source of truth: a later import that no longer lists an
        # entity deactivates it (so Claude never matches a phantom), without deleting its data.
        with TestSession() as db:
            result = EntityService().import_entities(db, [{"id": "drv-2", "name": ENTITY}])
            assert result["deactivated"] == 1, result
            explorent = EntityService().get_by_name(db, "B. Dailey - Explorent LLC")
            assert explorent is not None and explorent.active is False
            assert "B. Dailey - Explorent LLC" not in {e.entity_name for e in EntityService().list_active(db)}
            # A reappearing folder reactivates the entity.
            EntityService().import_entities(db, [{"id": "drv-2", "name": ENTITY}, {"id": "drv-1", "name": "B. Dailey - Explorent LLC"}])
            assert EntityService().get_by_name(db, "B. Dailey - Explorent LLC").active is True
            # Empty listing (API hiccup) must NOT deactivate the whole registry.
            guard = EntityService().import_entities(db, [])
            assert guard["deactivated"] == 0
            assert len(EntityService().list_active(db)) == 2

        # 6. Feedback learning.
        with TestSession() as db:
            mappings = db.execute(select(LearnedMapping)).scalars().all()
            sender_a = [m for m in mappings if m.pattern_type == "sender" and m.pattern_value == "ataurrehman3636@gmail.com"]
            assert sender_a and sender_a[0].source == "review_approve" and sender_a[0].entity == ENTITY
            assert not [m for m in mappings if m.pattern_type == "domain" and m.pattern_value == "gmail.com"]
            assert [m for m in mappings if m.pattern_type == "domain" and m.pattern_value == "ginsgroup.com"]
            sender_c = [m for m in mappings if m.pattern_type == "sender" and m.pattern_value == "jhoffman@ginsgroup.com"]
            assert sender_c and sender_c[0].source == "review_correct"

            top = LearningService().top_relevant(db, "Ata Ur Rehman <ataurrehman3636@gmail.com>", "Property tax receipt JSCD 761")
            assert top and top[0].pattern_type == "sender" and top[0].pattern_value == "ataurrehman3636@gmail.com"

            before_count, before_boost = sender_a[0].confirmation_count, sender_a[0].confidence_boost
            LearningService().record_review_mapping(db, "ataurrehman3636@gmail.com", "Property tax receipt JSCD 761", ENTITY, "Property Taxes", None, "review_approve")
            db.commit()
            again = db.execute(
                select(LearnedMapping).where(
                    LearnedMapping.pattern_type == "sender",
                    LearnedMapping.pattern_value == "ataurrehman3636@gmail.com",
                    LearnedMapping.entity == ENTITY,
                    LearnedMapping.level2 == "Property Taxes",
                )
            ).scalars().first()
            assert again.confirmation_count == before_count + 1
            assert again.confidence_boost > before_boost

        # Auto-sync guard: in offline/mock mode it must be a safe no-op (no Drive call, no error).
        with TestSession() as db:
            proc = ProcessingService()
            original_flag = proc.settings.enable_real_google
            proc.settings.enable_real_google = False
            try:
                before = len(EntityService().list_active(db))
                assert proc._sync_entities_from_drive(db) is False  # offline: no sync attempted
                assert len(EntityService().list_active(db)) == before
            finally:
                proc.settings.enable_real_google = original_flag

        print({"notifications": "ok", "review": "ok", "learning": "ok", "entity_import": "ok", "alias_on_correct": "ok", "auto_sync_guard": "ok"})
    finally:
        app.dependency_overrides.pop(get_db, None)
        FilingService.file_email_artifacts = original_file
        GmailService.mark_filed, GmailService.mark_skipped, GmailService.mark_failed = original_filed, original_skipped, original_failed
        test_engine.dispose()
        os.unlink(tmp.name)


if __name__ == "__main__":
    main()

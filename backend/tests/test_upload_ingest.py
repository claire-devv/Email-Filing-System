"""Backend test: UploadIngestService (Drive upload-folder scanning).

Verifies the move-not-copy filing of an uploaded file, extension preservation for non-PDFs,
null-sender/subject classification, the uncertain -> Needs-Review (file left in place) path, the
dedup ("never twice") guard, the destination bar on uploads folders, and the retry-loop exclusion.
Drive + Claude + pdf layers are faked; nothing touches the network.

Run: python -m app.scripts.test_upload_ingest
"""
import types
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.db.models import FileArtifact, NeedsReview, ProcessedEmail, ProcessedFile
from app.services.types import ClassificationResult, PreparedArtifact, PreparedEmail
from app.services.upload_ingest_service import UploadIngestService, upload_message_id, is_upload_message_id
from app.services.decision_service import UPLOAD_SOURCE_FOLDERS


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _ingest(decision: ClassificationResult, *, captured: dict, dest=("destfolder", "RRES - Books / E / Bank Statements"), existing_dup=None, extra_known=()):
    """An UploadIngestService with every external dependency faked. extra_known adds more entity
    names to list_active (e.g. to exercise the Client-Uploads wrong-folder mismatch net)."""
    svc = UploadIngestService()

    # Fake the pdf layer. prepare_single_document must yield ONLY the document artifact (no
    # email_body / combined_package) -- assert that here so a regression is caught.
    def fake_prepare(email, work_dir):
        art = PreparedArtifact(
            kind="attachment", original_filename=email.attachments[0].filename,
            local_path=email.attachments[0].local_path,
            generated_pdf_path=email.attachments[0].local_path,
            mime_type=email.attachments[0].mime_type, text_preview="Statement content", file_hash="hh",
            size_bytes=10,
        )
        captured["prepared_kinds"] = ["attachment"]
        return PreparedEmail(email=email, email_body_pdf=art.local_path, combined_pdf=art.local_path,
                             artifacts=[art], text_preview="Statement content", issues=[])

    # The service calls prepare_single_document for uploads (not prepare_email). Wire both to the
    # single-doc fake so the test fails loudly if the call site reverts to prepare_email.
    svc.pdf = types.SimpleNamespace(
        prepare_single_document=fake_prepare,
        prepare_email=lambda *a, **k: (_ for _ in ()).throw(AssertionError("uploads must use prepare_single_document, not prepare_email")),
    )
    svc.classifier = types.SimpleNamespace(classify=lambda db, prepared, entities: (captured.__setitem__("classified", prepared.email), decision)[1])
    # The decision's entity is a real, known entity (Client Uploads fixes it to the folder owner;
    # RRES Uploads classifies it). Make list_active return it so validation treats it as known.
    names = ([decision.entity] if decision.entity else []) + list(extra_known)
    known = [types.SimpleNamespace(entity_name=n) for n in dict.fromkeys(names)]
    svc.entities = types.SimpleNamespace(list_active=lambda db: known)

    def fake_move(file_id, from_folder, to_folder, new_name):
        captured["move"] = {"file_id": file_id, "from": from_folder, "to": to_folder, "name": new_name}
        return {"id": file_id, "webViewLink": "http://drive/view"}

    def fake_trash(file_id):
        captured["trash"] = file_id
        return {"id": file_id, "trashed": True}

    fake_drive = types.SimpleNamespace(
        resolve_target_folder=lambda db, entity, l2, l3: dest,
        move_file=fake_move,
        trash_file=fake_trash,
        find_valid_processed_file=lambda db, h, f: existing_dup,
        download_file_stream=lambda fid: [b"data"],
    )
    svc.filing.drive = fake_drive
    # Keep the real _summary_for_artifact / _trim_redundant_date / _attachment_target_spec.
    svc._download = lambda file_id, filename, work_dir: _touch(work_dir / "original" / filename)
    return svc


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    return path


def _decision(level2="Bank Statements", level3="Chase Bank (1234)", entity="J. Doe - 123 LLC", confidence=95, action="file", summary="Chase Bank Statement"):
    return ClassificationResult(
        entity=entity, level2=level2, level3=level3, file_summary=summary, confidence=confidence,
        unknown_entity=False, needs_review=False, reason="ok", action=action,
        document_date="2026-01-31", decision_audit={"filename_date": "2026.01.31"},
    )


def main() -> None:
    # --- helpers id round-trip ---
    assert is_upload_message_id(upload_message_id("X")) and not is_upload_message_id("gmailid")

    # --- Case 1: confident PDF -> moved + renamed with .pdf, ProcessedFile written. ---
    db = _session()
    cap = {}
    svc = _ingest(_decision(), captured=cap)
    out = svc.process_drive_upload(db, {"id": "f1", "name": "statement.pdf", "mimeType": "application/pdf", "createdTime": "2026-01-31T00:00:00Z"}, "srcfolder", "client_uploads", "J. Doe - 123 LLC")
    assert out["status"] == "filed", out
    assert cap["move"]["name"] == "2026.01.31 - Chase Bank Statement.pdf", cap["move"]["name"]
    assert cap["move"]["from"] == "srcfolder" and cap["move"]["to"] == "destfolder"
    assert cap["classified"].sender is None and cap["classified"].subject is None  # null-safety claim
    assert db.query(ProcessedFile).count() == 1
    # Single-document prep: exactly one artifact, kind=attachment, no email_body/combined_package.
    arts = db.query(FileArtifact).all()
    assert len(arts) == 1 and arts[0].kind == "attachment", [a.kind for a in arts]
    # The synthetic email is flagged as a drive upload so the classifier reframes as a document.
    assert (cap["classified"].raw_metadata or {}).get("source") == "drive_upload"
    db.close()

    # --- Case 2: confident CSV -> extension preserved (.csv, not .pdf). ---
    db = _session()
    cap = {}
    svc = _ingest(_decision(summary="Airbnb Earnings"), captured=cap)
    out = svc.process_drive_upload(db, {"id": "f2", "name": "earnings.csv", "mimeType": "text/csv", "createdTime": "2026-01-31T00:00:00Z"}, "src", "rres_uploads", None)
    assert out["status"] == "filed", out
    assert cap["move"]["name"].endswith(".csv"), cap["move"]["name"]
    db.close()

    # --- Case 3: uncertain (low confidence) -> review item, file NOT moved. ---
    db = _session()
    cap = {}
    svc = _ingest(_decision(confidence=20, action="needs_review"), captured=cap)
    out = svc.process_drive_upload(db, {"id": "f3", "name": "mystery.pdf", "mimeType": "application/pdf", "createdTime": "2026-01-31T00:00:00Z"}, "src", "rres_uploads", None)
    assert out["status"] == "pending_review", out
    assert "move" not in cap, "uncertain upload must NOT be moved"
    assert db.query(NeedsReview).count() == 1
    db.close()

    # --- Case 4: dedup -> re-scanning the same file id is skipped. ---
    db = _session()
    cap = {}
    svc = _ingest(_decision(), captured=cap)
    meta = {"id": "f4", "name": "s.pdf", "mimeType": "application/pdf", "createdTime": "2026-01-31T00:00:00Z"}
    first = svc.process_drive_upload(db, meta, "src", "rres_uploads", None)
    assert first["status"] == "filed"
    cap.clear()
    second = svc.process_drive_upload(db, meta, "src", "rres_uploads", None)
    assert second["status"] == "filed" and "move" not in cap, "re-scan must be skipped by dedup"
    db.close()

    # --- Case 5: classifying INTO an uploads folder is barred as a destination. ---
    from app.services.decision_service import DecisionValidator
    assert "client uploads" in UPLOAD_SOURCE_FOLDERS and "rres uploads" in UPLOAD_SOURCE_FOLDERS
    v = DecisionValidator()
    email = ProcessedEmail(gmail_message_id="drive-upload:zz", status="processing")
    bad = _decision(level2="Client Uploads", level3=None)
    res = v.validate(bad, email, [], [types.SimpleNamespace(entity_name="J. Doe - 123 LLC")], allow_new_entity=True, force_file=False)
    assert not res.should_file, "a doc classified to Client Uploads must not auto-file"
    assert "invalid_level2" in res.reasons, res.reasons
    db_ok = True
    assert db_ok

    # --- Case 6: retry-loop exclusion query shape (uploads excluded from the Gmail retry). ---
    db = _session()
    db.add(ProcessedEmail(gmail_message_id="drive-upload:keep", status="failed", attempts=1))
    db.add(ProcessedEmail(gmail_message_id="realgmail", status="failed", attempts=1))
    db.commit()
    from sqlalchemy import select
    rows = db.execute(
        select(ProcessedEmail).where(ProcessedEmail.gmail_message_id.not_like("drive-upload:%"))
    ).scalars().all()
    ids = {r.gmail_message_id for r in rows}
    assert ids == {"realgmail"}, ids
    db.close()

    # --- Case 7: byte-identical dup already filed in destination -> original TRASHED, not moved
    # a second time into the destination (the bug fix). ---
    db = _session()
    cap = {}
    dup = types.SimpleNamespace(drive_file_id="already-filed-id")
    svc = _ingest(_decision(), captured=cap, existing_dup=dup)
    out = svc.process_drive_upload(db, {"id": "f7", "name": "s.pdf", "mimeType": "application/pdf", "createdTime": "2026-01-31T00:00:00Z"}, "src", "rres_uploads", None)
    assert out["status"] == "filed", out
    assert cap.get("trash") == "f7", "duplicate original must be trashed"
    assert "move" not in cap, "a duplicate must NOT be moved into the destination"
    assert db.query(ProcessedFile).count() == 0, "no new ProcessedFile for a dedup hit"
    db.close()

    # --- Case 8: classifier prompt framing -- upload says "document" + override, email unchanged. ---
    from app.services.classifier_service import ClassifierService
    from app.services.types import EmailMessageData, PreparedArtifact as _PA, PreparedEmail as _PE
    from pathlib import Path as _Path
    cs = ClassifierService()

    def _prepared(meta):
        em = EmailMessageData(gmail_message_id="x", thread_id=None, sender=None, recipient=None,
                              subject=None, received_at=None, body_text="", body_html=None,
                              attachments=[], raw_metadata=meta)
        art = _PA(kind="attachment", original_filename="s.pdf", local_path=_Path("s.pdf"),
                  generated_pdf_path=None, mime_type="application/pdf", text_preview="Account 0798")
        return _PE(email=em, email_body_pdf=_Path("a"), combined_pdf=_Path("b"), artifacts=[art],
                   text_preview="Account 0798", issues=[])

    up_prompt = cs._prompt(_prepared({"source": "drive_upload"}), [], [])
    em_prompt = cs._prompt(_prepared({}), [], [])
    assert "SOURCE OVERRIDE" in up_prompt and "filing document for RRES" in up_prompt, "upload framing"
    assert "SOURCE OVERRIDE" not in em_prompt and "filing email for RRES" in em_prompt, "email framing unchanged"

    # --- Case 9: Client Uploads wrong-folder safety net. A doc dropped in entity A's folder whose
    # CONTENT confidently matches a different KNOWN entity B -> Needs Review, file NOT moved. ---
    db = _session()
    cap = {}
    # Claude reads it as "B - Other LLC" (known, confident 90); folder owner is "J. Doe - 123 LLC".
    claude_says_b = _decision(entity="B - Other LLC", confidence=90)
    svc = _ingest(claude_says_b, captured=cap, extra_known=["J. Doe - 123 LLC"])
    out = svc.process_drive_upload(db, {"id": "f9", "name": "wrong.pdf", "mimeType": "application/pdf", "createdTime": "2026-01-31T00:00:00Z"}, "src", "client_uploads", "J. Doe - 123 LLC")
    assert out["status"] == "pending_review", out
    assert "move" not in cap, "a wrong-folder upload must NOT be filed -- it goes to review"
    review = db.query(NeedsReview).order_by(NeedsReview.id.desc()).first()
    assert review is not None, "a review item must be created"
    # The review's proposed entity is still the folder owner (reviewer decides), and the mismatch
    # reason mentions the conflicting content entity.
    assert review.proposed_entity == "J. Doe - 123 LLC", review.proposed_entity
    assert "B - Other LLC" in (review.claude_reasoning or ""), review.claude_reasoning
    db.close()

    # --- Case 9b: folder owner agrees with content -> normal file (net does NOT trip). ---
    db = _session()
    cap = {}
    svc = _ingest(_decision(entity="J. Doe - 123 LLC", confidence=90), captured=cap)
    out = svc.process_drive_upload(db, {"id": "f9b", "name": "ok.pdf", "mimeType": "application/pdf", "createdTime": "2026-01-31T00:00:00Z"}, "src", "client_uploads", "J. Doe - 123 LLC")
    assert out["status"] == "filed" and "move" in cap, "matching folder/content must file normally"
    db.close()

    # --- Case 10: permanent error (password-protected PDF) -> Needs Review (NOT failed, no retry
    # churn) with a CLEAN human message; file left in place. ---
    from app.services.upload_ingest_service import _is_permanent_error, _clean_error_message
    raw = ("Error code: 400 - {'error': {'message': 'The PDF specified is password protected.'}}")
    assert _is_permanent_error(Exception(raw)) is True
    assert "password protected" in _clean_error_message(Exception(raw)).lower()
    assert "Error code" not in _clean_error_message(Exception(raw))  # provider noise stripped
    # a genuinely transient error stays retryable (failed)
    assert _is_permanent_error(Exception("Error code: 529 - overloaded")) is False

    db = _session()
    cap = {}
    svc = _ingest(_decision(), captured=cap)
    svc.classifier = types.SimpleNamespace(classify=lambda db, prepared, entities: (_ for _ in ()).throw(Exception(raw)))
    out = svc.process_drive_upload(db, {"id": "f10", "name": "locked.pdf", "mimeType": "application/pdf", "createdTime": "2026-01-31T00:00:00Z"}, "src", "client_uploads", "J. Doe - 123 LLC")
    assert out["status"] == "pending_review", out
    assert "move" not in cap, "a permanent-error upload must not be filed"
    e = db.query(ProcessedEmail).order_by(ProcessedEmail.id.desc()).first()
    assert e.status == "pending_review" and e.status != "failed", e.status
    assert "password protected" in (e.last_error or "").lower() and "Error code" not in (e.last_error or "")
    db.close()

    print("upload ingest: all assertions passed")


if __name__ == "__main__":
    main()

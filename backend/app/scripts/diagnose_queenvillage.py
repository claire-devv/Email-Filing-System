"""Read-only: show exactly what produced the duplicate/mis-pathed Queen Village FilingLog rows.

Run on the server:  venv/bin/python -m app.scripts.diagnose_queenvillage
"""
from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import FilingLog, ProcessedEmail, FileArtifact, NeedsReview


def _safe(s, n):
    return (str(s or "")[:n]).encode("ascii", "replace").decode("ascii")


db = SessionLocal()
try:
    logs = db.execute(
        select(FilingLog).where(FilingLog.subject.ilike("%Queen Village open balances%"))
        .order_by(FilingLog.id.desc()).limit(10)
    ).scalars().all()
    print(f"FilingLog rows for 'Queen Village open balances': {len(logs)}")
    email_ids = set()
    for l in logs:
        email_ids.add(l.email_id)
        print(f"  log id={l.id} email_id={l.email_id} status={l.status} entity={l.entity!r}")
        print(f"      folder={_safe(l.folder_path, 90)!r}  created={l.created_at}")

    for eid in email_ids:
        e = db.get(ProcessedEmail, eid)
        if not e:
            continue
        meta = e.metadata_json or {}
        aud = meta.get("decision_audit") or {}
        final = meta.get("final") or {}
        print("\n" + "=" * 80)
        print(f"EMAIL id={eid} status={e.status}")
        print(f"  final = {final}")
        print(f"  audit.additional_entities  = {aud.get('additional_entities')}")
        print(f"  audit.auto_split_entities  = {aud.get('auto_split_entities')}")
        print(f"  audit.split_filed          = {aud.get('split_filed')}")
        print(f"  audit.reasons              = {aud.get('reasons')}")
        print(f"  metadata.split             = {meta.get('split')}")
        review = db.execute(select(NeedsReview).where(NeedsReview.email_id == eid).order_by(NeedsReview.id.desc())).scalars().first()
        if review:
            print(f"  review status={review.status} reviewer_decision={review.reviewer_decision} corrected_entity={review.corrected_entity!r}")
        print("  artifacts:")
        for a in db.execute(select(FileArtifact).where(FileArtifact.email_id == eid)).scalars().all():
            print(f"    [{a.status:14s}] {a.kind:16s} {_safe(a.original_filename, 40)!r} folder_id={a.drive_folder_id}")
finally:
    db.close()

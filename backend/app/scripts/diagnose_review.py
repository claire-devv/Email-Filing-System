"""Read-only: explain WHY the most recent pending Needs-Review items were held.

Run on the server:  .venv/bin/python -m app.scripts.diagnose_review [N]
Shows, per pending item: the reasons the gate recorded, the primary entity, Claude's
additional_entities, and each attachment's resolved entity/confidence — enough to see exactly which
check routed it to review. Touches nothing.
"""
import sys
from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import NeedsReview, ProcessedEmail, FileArtifact, Entity
from app.services.filing_service import resolve_artifact_entity

N = int(sys.argv[1]) if len(sys.argv) > 1 else 6
db = SessionLocal()
try:
    known = {e.entity_name for e in db.execute(select(Entity).where(Entity.active.is_(True))).scalars()}
    items = db.execute(
        select(NeedsReview).where(NeedsReview.status == "pending").order_by(NeedsReview.id.desc()).limit(N)
    ).scalars().all()
    for it in items:
        e = db.get(ProcessedEmail, it.email_id)
        meta = (e.metadata_json or {}) if e else {}
        aud = meta.get("decision_audit") or {}
        print("=" * 90)
        print(f"id={it.id}  subject={ (e.subject if e else '')[:60]!r}")
        print(f"  proposed_entity = {it.proposed_entity!r}  ({'known' if it.proposed_entity in known else 'UNKNOWN'})")
        print(f"  confidence      = {it.confidence}")
        print(f"  gate reasons    = {aud.get('reasons')}")
        print(f"  additional_entities (Claude) = {aud.get('additional_entities')}")
        print(f"  auto_split_entities          = {aud.get('auto_split_entities')}")
        arts = db.execute(select(FileArtifact).where(FileArtifact.email_id == it.email_id)).scalars().all()
        print("  per-attachment resolution:")
        for a in arts:
            if a.kind in ("email_body", "combined_package"):
                continue
            ent, conf, matched = resolve_artifact_entity(aud, a)
            k = "known" if ent in known else ("—" if not ent else "UNKNOWN")
            print(f"    [{a.status:12s}] {(a.original_filename or a.kind)[:44]:44s} -> {ent!r} conf={conf} matched={matched} ({k})")
        print()
finally:
    db.close()

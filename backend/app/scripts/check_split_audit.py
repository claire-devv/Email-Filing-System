from app.db.session import SessionLocal
from app.db.models import NeedsReview

db = SessionLocal()
items = db.query(NeedsReview).filter(NeedsReview.status == 'pending').order_by(NeedsReview.id.desc()).limit(3).all()
for item in items:
    meta = item.metadata_json or {}
    audit = meta.get('decision_audit', {})
    summaries = audit.get('artifact_summaries', {})
    print(f'=== item id={item.id} subject={item.email.subject if item.email else "?"!r}')
    print(f'  artifact_summaries:')
    for k, v in summaries.items():
        if isinstance(v, dict):
            print(f'    [{k}] summary={v.get("summary")!r} entity={v.get("entity")!r}')
        else:
            print(f'    [{k}] = {v!r}')
    print()
db.close()

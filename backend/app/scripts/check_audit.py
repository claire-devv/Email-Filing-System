from app.db.session import SessionLocal
from app.db.models import NeedsReview

db = SessionLocal()
item = db.query(NeedsReview).filter(NeedsReview.status == 'pending').order_by(NeedsReview.id.desc()).first()
if not item:
    print("No pending review items found.")
else:
    print(f"Review item id={item.id}, proposed_entity={item.proposed_entity}")
    audit = (item.metadata_json or {}).get('decision_audit', {})
    print('additional_entities:', audit.get('additional_entities'))
    print('auto_split_entities:', audit.get('auto_split_entities'))
    ac = audit.get('artifact_classifications', {})
    print('artifact_classifications:')
    for k, v in ac.items():
        if isinstance(v, dict):
            print(f'  [{k}] entity={v.get("entity")} entity_confidence={v.get("entity_confidence")} level2={v.get("level2")}')
        else:
            print(f'  [{k}] = {v}')

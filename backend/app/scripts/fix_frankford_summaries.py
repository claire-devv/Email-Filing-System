"""
One-off: fix artifact_summaries for the 1322 Frankford multi-entity review item
where all 4 attachments got the same summary "May 2026 Owner Report".
"""
import copy
from app.db.session import SessionLocal
from app.db.models import NeedsReview
from sqlalchemy.orm.attributes import flag_modified

CORRECT_SUMMARIES = {
    '1322 Frankford Owner LLC - May 2026 Owner Report.pdf': '1322 Frankford Owner Report',
    '2002 Frankford Owner LLC - May 2026 Owner Report.pdf': '2002 Frankford Owner Report',
    '1603 Frankford Owner LLC - May 2026 Owner Report.pdf': '1603 Frankford Owner Report',
    'Queen Village Owner LLC - May 2026 Owner Report.pdf': 'Queen Village Owner Report',
}

db = SessionLocal()
items = db.query(NeedsReview).filter(NeedsReview.status == 'pending').all()

for item in items:
    meta = copy.deepcopy(item.metadata_json or {})
    audit = meta.get('decision_audit', {})
    summaries = audit.get('artifact_summaries', {})
    if not summaries:
        continue
    keys = set(CORRECT_SUMMARIES.keys())
    if not keys.issubset(set(summaries.keys())):
        continue
    values = {summaries[k] for k in keys}
    if len(values) != 1:
        continue
    print(f'Patching item id={item.id}')
    for k, v in CORRECT_SUMMARIES.items():
        summaries[k] = v
    audit['artifact_summaries'] = summaries
    meta['decision_audit'] = audit
    item.metadata_json = meta
    flag_modified(item, 'metadata_json')

db.commit()
db.close()
print('Done.')

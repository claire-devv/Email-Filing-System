"""
One-off: backfill auto_split_entities into pending NeedsReview items where
artifact_classifications has multiple distinct entities but auto_split_entities is missing.
Safe to run multiple times (idempotent).
"""
from app.db.session import SessionLocal
from app.db.models import NeedsReview, FileArtifact
from app.services.filing_service import resolve_artifact_entity

db = SessionLocal()
items = db.query(NeedsReview).filter(NeedsReview.status == 'pending').all()
patched = 0

for item in items:
    meta = item.metadata_json or {}
    audit = meta.get('decision_audit', {})
    if audit.get('auto_split_entities') is not None:
        continue  # already set, skip

    artifacts = db.query(FileArtifact).filter(FileArtifact.email_id == item.email_id).all()
    primary = item.proposed_entity
    distinct = set()
    if primary:
        distinct.add(primary)

    for artifact in artifacts:
        if getattr(artifact, 'kind', None) in {'combined_package', 'email_body'}:
            continue
        if getattr(artifact, 'status', None) == 'unsupported':
            continue
        entity, _, matched = resolve_artifact_entity(audit, artifact)
        if matched and entity:
            distinct.add(entity)

    additional = audit.get('additional_entities') or []
    is_multi = bool(additional) or len(distinct) > 1
    if not is_multi:
        continue

    audit['auto_split_entities'] = sorted(distinct)
    item.metadata_json = {**meta, 'decision_audit': audit}
    patched += 1
    print(f'  Patched item id={item.id}: auto_split_entities={audit["auto_split_entities"]}')

db.commit()
db.close()
print(f'\nDone. {patched} item(s) patched.')

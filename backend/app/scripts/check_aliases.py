from app.db.session import SessionLocal
from app.db.models import Entity, LearnedMapping

db = SessionLocal()

print('--- Learned mappings touching Foremost/Stowers/cast-cap ---')
all_m = db.query(LearnedMapping).filter(LearnedMapping.active == True).all()
for m in all_m:
    combined = ' '.join(filter(None, [m.pattern_type, m.pattern_value, m.entity])).lower()
    if any(k in combined for k in ['foremost', 'foremos', 'stowers', 'cast-cap', 'morriss']):
        print(f'  type={m.pattern_type!r} value={m.pattern_value!r} entity={m.entity!r} boost={m.confidence_boost} count={m.confirmation_count}')

print()
print('--- All learned mappings ---')
for m in all_m:
    print(f'  type={m.pattern_type!r} value={m.pattern_value!r} entity={m.entity!r} boost={m.confidence_boost} count={m.confirmation_count}')

db.close()

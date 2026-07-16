Alembic migration folder for the Phase 1 backend.

Local MVP can initialize with:

```powershell
python -m app.scripts.init_db
```

When schema changes stabilize:

```powershell
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

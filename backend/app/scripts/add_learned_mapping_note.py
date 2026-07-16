"""One-time, idempotent column add for learned_mappings.note.

The app provisions schema via Base.metadata.create_all and has no Alembic history, so
create_all will add the `note` column to *fresh* databases automatically but will NOT
alter an already-created table. Run this once against an existing runtime DB. Safe to
re-run (it no-ops if the column already exists or the table isn't created yet).

    cd backend; .venv\\Scripts\\python.exe -m app.scripts.add_learned_mapping_note
"""
from sqlalchemy import inspect, text

from app.db.session import engine

TABLE = "learned_mappings"
COLUMN = "note"


def main() -> None:
    inspector = inspect(engine)
    if TABLE not in inspector.get_table_names():
        print(f"{TABLE} does not exist yet; create_all will include {COLUMN}. Nothing to do.")
        return
    if any(col["name"] == COLUMN for col in inspector.get_columns(TABLE)):
        print(f"{TABLE}.{COLUMN} already present; nothing to do.")
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT"))
    print(f"Added {TABLE}.{COLUMN}.")


if __name__ == "__main__":
    main()

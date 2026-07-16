"""One-off maintenance: deactivate any domain-level learned mappings for public
free-mail providers (e.g. gmail.com), which would wrongly generalize one client to
every sender on that provider. Exact-sender and keyword mappings are left untouched.

Run:
    .\\.venv\\Scripts\\python.exe -m app.scripts.cleanup_freemail_mappings
"""

from sqlalchemy import select

from app.db.models import LearnedMapping
from app.db.session import SessionLocal, init_db
from app.services.learning_service import FREE_MAIL_DOMAINS


def main() -> None:
    init_db()
    with SessionLocal() as db:
        rows = db.execute(
            select(LearnedMapping).where(
                LearnedMapping.pattern_type == "domain",
                LearnedMapping.active.is_(True),
            )
        ).scalars().all()
        deactivated = []
        for row in rows:
            if (row.pattern_value or "").strip().lower() in FREE_MAIL_DOMAINS:
                row.active = False
                deactivated.append(row.pattern_value)
        if not deactivated:
            print("No active free-mail domain mappings found. Nothing to clean up.")
            return
        db.commit()
        print(f"Deactivated {len(deactivated)} free-mail domain mapping(s): {sorted(set(deactivated))}")


if __name__ == "__main__":
    main()

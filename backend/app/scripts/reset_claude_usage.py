from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import ApiUsage
from app.db.session import SessionLocal, init_db


def main() -> None:
    init_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with SessionLocal() as db:
        usage = db.execute(
            select(ApiUsage).where(ApiUsage.provider == "claude", ApiUsage.usage_date == today)
        ).scalars().first()
        if not usage:
            print("No Claude usage row for today.")
            return
        usage.call_count = 0
        db.commit()
        print("Claude usage reset for today.")


if __name__ == "__main__":
    main()

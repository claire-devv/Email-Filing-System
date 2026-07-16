from sqlalchemy import select

from app.db.models import ApiUsage
from app.db.session import SessionLocal, init_db


def main() -> None:
    init_db()
    with SessionLocal() as db:
        rows = db.execute(select(ApiUsage).order_by(ApiUsage.usage_date.desc(), ApiUsage.provider)).scalars().all()
        if not rows:
            print("No API usage recorded.")
            return
        for row in rows:
            print(f"{row.usage_date} {row.provider}: {row.call_count}")


if __name__ == "__main__":
    main()

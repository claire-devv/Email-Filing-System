"""One-off maintenance: re-normalize ProcessedEmail.received_at to UTC.

Emails processed before the gmail_service UTC fix stored received_at as the sender's
local wall-clock (SQLite drops the timezone offset), while emails processed after store
true UTC. Mixed clocks make the Needs Review / Activity lists sort and display wrong.

The original "Date:" header is preserved in metadata_json["headers"]["date"], so we can
recompute received_at correctly (parse the header, convert to UTC) with no data loss and
no Gmail call. Rows without a usable Date header are left untouched.

Run from the backend/ directory:
    .\\.venv\\Scripts\\python.exe -m app.scripts.normalize_received_at
"""

from datetime import timezone
from email.utils import parsedate_to_datetime

from sqlalchemy import select

from app.db.models import ProcessedEmail
from app.db.session import SessionLocal, init_db


def _utc_naive_from_header(date_header: str):
    """Parse an email Date header to a naive-UTC datetime (matching how SQLite stores it)."""
    parsed = parsedate_to_datetime(date_header)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def main() -> None:
    init_db()
    with SessionLocal() as db:
        rows = db.execute(select(ProcessedEmail)).scalars().all()
        changed = 0
        skipped_no_header = 0
        unchanged = 0
        for row in rows:
            headers = (row.metadata_json or {}).get("headers") or {}
            date_header = headers.get("date")
            if not date_header:
                skipped_no_header += 1
                continue
            try:
                corrected = _utc_naive_from_header(date_header)
            except Exception:
                skipped_no_header += 1
                continue
            current = row.received_at
            if current is not None and current.tzinfo is not None:
                current = current.astimezone(timezone.utc).replace(tzinfo=None)
            if current == corrected:
                unchanged += 1
                continue
            row.received_at = corrected
            db.add(row)
            changed += 1
        db.commit()
        print(
            f"received_at normalized: {changed} updated, {unchanged} already correct, "
            f"{skipped_no_header} skipped (no/invalid Date header), {len(rows)} total."
        )


if __name__ == "__main__":
    main()

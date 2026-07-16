"""Maintenance: park the API-limit/failed backlog so the retry loop stops spending Claude
calls on it, and (optionally) reset today's Claude usage counter so a NEW test email can run.

Usage (from the backend/ directory):
    python -m app.scripts.park_waiting_emails              # park backlog + reset today's counter
    python -m app.scripts.park_waiting_emails --no-reset   # park backlog only
    python -m app.scripts.park_waiting_emails --dry-run     # report only, change nothing

"Park" sets the email's status to "skipped" (a terminal status), so:
  - the retry loop (_retry_failed_emails_once) never selects it again, and
  - process_message short-circuits it if it is ever re-seen.
It does NOT touch Gmail labels or Drive — purely a local DB status change.
"""
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import ApiUsage, ProcessedEmail
from app.db.session import SessionLocal

# Statuses that the retry loop keeps re-processing (and thus re-spending API calls on).
BACKLOG_STATUSES = ["waiting_api_limit", "failed"]
PARKED_STATUS = "skipped"


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    reset_usage = "--no-reset" not in sys.argv

    db = SessionLocal()
    try:
        rows = db.execute(
            select(ProcessedEmail).where(ProcessedEmail.status.in_(BACKLOG_STATUSES))
        ).scalars().all()

        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1

        print(f"Backlog found: {counts or '{}'} (total {len(rows)})")

        if not dry_run:
            for row in rows:
                row.status = PARKED_STATUS
                row.last_error = f"Parked from {row.last_error or row.status!r} to skip retries."
            db.commit()
            print(f"Parked {len(rows)} email(s) -> '{PARKED_STATUS}'.")

        if reset_usage:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            usage = db.execute(
                select(ApiUsage).where(ApiUsage.provider == "claude", ApiUsage.usage_date == today)
            ).scalars().first()
            before = usage.call_count if usage else 0
            print(f"Claude usage today ({today}): {before}")
            if usage and not dry_run:
                usage.call_count = 0
                db.commit()
                print("Reset Claude usage counter to 0.")
        if dry_run:
            print("Dry run: no changes written.")
    finally:
        db.close()


if __name__ == "__main__":
    main()

"""
Read-only: list every email/upload currently sitting in 'failed' or 'waiting_api_limit',
split into two buckets:

  - RETRYING: attempts < 8 and not flagged permanent_failure -- the background retry loop
    (backend/app/main.py) will pick these up on its own (failed: every 5 min, waiting_api_limit:
    every 2 h). No action needed unless you want it filed sooner.
  - STUCK: attempts >= 8, or flagged permanent_failure (e.g. password-protected/corrupt
    attachment) -- the retry loop has given up. These need manual attention: open the email,
    fix/remove the problem attachment, then re-run with --retry <id> to give it one more attempt,
    or file it by hand.

Touches nothing by default. Run from backend/:
    .venv\\Scripts\\python.exe -m app.scripts.list_stuck_emails
    .venv\\Scripts\\python.exe -m app.scripts.list_stuck_emails --retry 123   # reset attempts=0
                                                                                #   on email id 123
                                                                                #   so it retries
                                                                                #   on the next loop
"""
import sys

from sqlalchemy import select

from app.db.models import ProcessedEmail
from app.db.session import SessionLocal

_MAX_AUTO_RETRY_ATTEMPTS = 8


def main() -> None:
    if "--retry" in sys.argv:
        idx = sys.argv.index("--retry")
        email_id = int(sys.argv[idx + 1])
        db = SessionLocal()
        try:
            row = db.get(ProcessedEmail, email_id)
            if not row:
                print(f"No email with id={email_id}.")
                return
            row.attempts = 0
            meta = dict(row.metadata_json or {})
            meta.pop("permanent_failure", None)
            row.metadata_json = meta
            db.commit()
            print(f"Reset id={email_id} attempts to 0 and cleared permanent_failure. "
                  "It will be retried on the next retry-loop pass.")
        finally:
            db.close()
        return

    db = SessionLocal()
    try:
        rows = db.execute(
            select(ProcessedEmail)
            .where(ProcessedEmail.status.in_(["failed", "waiting_api_limit"]))
            .order_by(ProcessedEmail.updated_at.desc())
        ).scalars().all()

        if not rows:
            print("Nothing currently failed or waiting on the API limit.")
            return

        stuck = []
        retrying = []
        for row in rows:
            permanent = bool((row.metadata_json or {}).get("permanent_failure"))
            if permanent or row.attempts >= _MAX_AUTO_RETRY_ATTEMPTS:
                stuck.append(row)
            else:
                retrying.append(row)

        def _fmt(row: ProcessedEmail) -> str:
            kind = "upload" if row.gmail_message_id.startswith("drive-upload:") else "email"
            err = (row.last_error or "").strip().replace("\n", " ")[:140]
            return (
                f"  id={row.id:<6} [{kind:5s}] status={row.status:18s} attempts={row.attempts} "
                f"updated={row.updated_at}\n"
                f"           subject={ (row.subject or '')[:70]!r}\n"
                f"           sender={ (row.sender or '')[:60]!r}\n"
                f"           error={err!r}"
            )

        print(f"STUCK (retry loop has given up, {len(stuck)}):")
        if not stuck:
            print("  none")
        for row in stuck:
            print(_fmt(row))
            print()

        print(f"\nRETRYING (will auto-recover on its own, {len(retrying)}):")
        if not retrying:
            print("  none")
        for row in retrying:
            print(_fmt(row))
            print()

        if stuck:
            print(f"To retry a stuck one: .venv\\Scripts\\python.exe -m app.scripts.list_stuck_emails --retry <id>")
    finally:
        db.close()


if __name__ == "__main__":
    main()

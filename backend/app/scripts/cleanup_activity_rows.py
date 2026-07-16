"""
Remove SPECIFIC rows from the Activity feed by ID (deployed-app cleanup). DB-only: never moves,
deletes, or changes anything in Google Drive. Files already filed stay exactly where they are.

Workflow:
  1) LIST the recent activity rows (with their FilingLog ids) so you can pick the exact ones:
       .venv/bin/python -m app.scripts.cleanup_activity_rows --list
       .venv/bin/python -m app.scripts.cleanup_activity_rows --list --status pending_review
       .venv/bin/python -m app.scripts.cleanup_activity_rows --list --status failed
  2) DELETE only the ids you chose (comma-separated). Dry run first (no --apply):
       .venv/bin/python -m app.scripts.cleanup_activity_rows --ids 123,124,130
       .venv/bin/python -m app.scripts.cleanup_activity_rows --ids 123,124,130 --apply

This deletes ONLY the exact FilingLog rows whose ids you pass -- nothing else is touched. It does
NOT delete ProcessedEmail records or NeedsReview items; it only removes the activity-feed row.
"""
import sys

from sqlalchemy import select

from app.db.session import SessionLocal
from app.db.models import FilingLog, ProcessedEmail


def _safe(s: object, n: int) -> str:
    return (str(s or "")[:n]).encode("ascii", "replace").decode("ascii")


def _arg(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main() -> None:
    db = SessionLocal()
    try:
        if "--list" in sys.argv:
            status = _arg("--status")
            stmt = select(FilingLog).order_by(FilingLog.created_at.desc()).limit(80)
            if status:
                stmt = select(FilingLog).where(FilingLog.status == status).order_by(FilingLog.created_at.desc()).limit(80)
            rows = db.execute(stmt).scalars().all()
            print(f"{'LOG_ID':>7}  {'STATUS':16}  {'DATE':16}  SUBJECT")
            print("-" * 90)
            for l in rows:
                ts = l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else "?"
                print(f"{l.id:>7}  {l.status:16}  {ts:16}  {_safe(l.subject, 50)}")
            print("\nPick the LOG_IDs you want to remove, then run:")
            print("  ... --ids <id1,id2,...>            (dry run)")
            print("  ... --ids <id1,id2,...> --apply    (delete)")
            return

        ids_arg = _arg("--ids")
        if not ids_arg:
            print("Nothing to do. Use --list first, then --ids <id1,id2,...> [--apply].")
            return
        try:
            ids = [int(x) for x in ids_arg.replace(" ", "").split(",") if x]
        except ValueError:
            print("--ids must be a comma-separated list of integers, e.g. --ids 123,124,130")
            return

        rows = db.execute(select(FilingLog).where(FilingLog.id.in_(ids))).scalars().all()
        found = {l.id for l in rows}
        missing = [i for i in ids if i not in found]
        print(f"Matched {len(rows)} FilingLog row(s) for ids {ids}:")
        for l in rows:
            e = db.get(ProcessedEmail, l.email_id) if l.email_id else None
            print(f"  id={l.id} status={l.status} email_status={(e.status if e else None)} :: {_safe(l.subject, 55)}")
        if missing:
            print(f"  (no rows found for ids: {missing})")

        if "--apply" not in sys.argv:
            print("\n[DRY RUN] Nothing deleted. Add --apply to delete exactly these rows.")
            return

        for l in rows:
            db.delete(l)
        db.commit()
        print(f"\nDeleted {len(rows)} activity row(s). (No Drive files, no email records, no reviews were touched.)")
    finally:
        db.close()


if __name__ == "__main__":
    main()

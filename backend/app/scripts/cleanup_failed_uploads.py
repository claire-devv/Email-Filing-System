"""
Remove FAILED Drive-upload rows from the activity feed (deployed-app cleanup).

Scope: ONLY uploads (ProcessedEmail.gmail_message_id LIKE 'drive-upload:%') whose FilingLog rows
have status='failed'. Email rows are never touched. This is a DB-only cleanup — it does NOT move,
delete, or change anything in Google Drive. The uploaded files stay exactly where they are.

SAFE BY DEFAULT: runs a DRY RUN and prints what it would delete. Nothing is deleted unless you
pass --apply. Optionally --purge-emails also removes the underlying failed ProcessedEmail rows
(and their artifacts) so the retry loop can't re-pick them; without it, only the activity-feed
FilingLog rows are removed.

Usage (from backend/):
    .venv\\Scripts\\python.exe -m app.scripts.cleanup_failed_uploads              # dry run
    .venv\\Scripts\\python.exe -m app.scripts.cleanup_failed_uploads --apply       # delete failed upload FilingLogs
    .venv\\Scripts\\python.exe -m app.scripts.cleanup_failed_uploads --apply --purge-emails
"""
import sys

from sqlalchemy import select

from app.db.session import SessionLocal
from app.db.models import FilingLog, ProcessedEmail, FileArtifact


def main() -> None:
    apply = "--apply" in sys.argv
    purge_emails = "--purge-emails" in sys.argv

    db = SessionLocal()
    try:
        # Upload email_ids (drive-upload:* rows).
        upload_email_ids = set(
            db.execute(
                select(ProcessedEmail.id).where(ProcessedEmail.gmail_message_id.like("drive-upload:%"))
            ).scalars().all()
        )
        if not upload_email_ids:
            print("No Drive-upload rows found. Nothing to do.")
            return

        # Failed FilingLog rows that belong to uploads.
        failed_logs = db.execute(
            select(FilingLog).where(
                FilingLog.status == "failed",
                FilingLog.email_id.in_(upload_email_ids),
            )
        ).scalars().all()

        print(f"Failed UPLOAD FilingLog rows found: {len(failed_logs)}")
        for l in failed_logs[:50]:
            print(f"  log id={l.id} email_id={l.email_id} subject={ (l.subject or '')[:50]!r} msg={ (l.message or '')[:60]!r}")
        if len(failed_logs) > 50:
            print(f"  ... and {len(failed_logs) - 50} more")

        # Which underlying ProcessedEmail upload rows are currently status='failed'?
        failed_upload_emails = db.execute(
            select(ProcessedEmail).where(
                ProcessedEmail.id.in_(upload_email_ids),
                ProcessedEmail.status == "failed",
            )
        ).scalars().all()
        print(f"\nUnderlying UPLOAD ProcessedEmail rows still in 'failed' status: {len(failed_upload_emails)}")
        for e in failed_upload_emails[:50]:
            up = (e.metadata_json or {}).get("upload") or {}
            print(f"  email id={e.id} attempts={e.attempts} file={ (up.get('original_filename') or '')[:50]!r}")

        if not apply:
            print("\n[DRY RUN] Nothing deleted. Re-run with --apply to delete the failed upload"
                  " FilingLog rows" + (" AND the failed ProcessedEmail rows." if purge_emails else ".")
                  + ("\n         (add --purge-emails to also remove the underlying failed upload rows.)"
                     if not purge_emails else ""))
            return

        # DELETE failed upload FilingLog rows.
        for l in failed_logs:
            db.delete(l)
        print(f"\nDeleted {len(failed_logs)} failed upload FilingLog rows.")

        if purge_emails:
            purged = 0
            for e in failed_upload_emails:
                # Remove its artifacts + any remaining logs, then the email row itself.
                for a in db.execute(select(FileArtifact).where(FileArtifact.email_id == e.id)).scalars().all():
                    db.delete(a)
                for l in db.execute(select(FilingLog).where(FilingLog.email_id == e.id)).scalars().all():
                    db.delete(l)
                db.delete(e)
                purged += 1
            print(f"Purged {purged} failed upload ProcessedEmail rows (+ their artifacts/logs).")

        db.commit()
        print("\nDone. (No Google Drive files were touched.)")
    finally:
        db.close()


if __name__ == "__main__":
    main()

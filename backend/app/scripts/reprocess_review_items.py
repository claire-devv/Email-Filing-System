"""
Re-run specific Needs-Review items through the CURRENT code (deployed-app maintenance).

Why: normal processing skips anything already 'pending_review' (it's parked for a human), so a
Needs-Review item never re-runs on its own. This resets the chosen items and re-processes them, so
they go through the updated PDF-prep / classifier / decision-gate logic -- some may now auto-file,
some may re-land in review with the new (clearer) reasons.

Requirements: the original Gmail message must still exist in the connected inbox (it re-fetches),
and each item spends one Claude classification call.

Workflow (from backend/):
  1) LIST current pending items to get their review ids + gmail message ids:
       .venv/bin/python -m app.scripts.reprocess_review_items --list
  2) DRY RUN the chosen ids (shows what it would reset, does nothing):
       .venv/bin/python -m app.scripts.reprocess_review_items --ids 12,15
  3) APPLY -- reset + reprocess exactly those:
       .venv/bin/python -m app.scripts.reprocess_review_items --ids 12,15 --apply

Only the items whose NeedsReview ids you pass are touched. Uses the same process_message() the
live pipeline uses, so filing/dedup/move behaviour is identical to a fresh email.
"""
import sys

from sqlalchemy import select

from app.db.session import SessionLocal
from app.db.models import NeedsReview, ProcessedEmail, FileArtifact, FilingLog


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
            items = db.execute(
                select(NeedsReview).where(NeedsReview.status == "pending").order_by(NeedsReview.id.desc())
            ).scalars().all()
            print(f"{'REVIEW_ID':>9}  {'EMAIL_ID':>8}  {'CONF':>5}  SUBJECT")
            print("-" * 90)
            for it in items:
                e = db.get(ProcessedEmail, it.email_id)
                print(f"{it.id:>9}  {it.email_id:>8}  {str(it.confidence or ''):>5}  {_safe(e.subject if e else '', 55)}")
            print("\nPick REVIEW_IDs, then: --ids <id1,id2> [--apply]")
            return

        ids_arg = _arg("--ids")
        if not ids_arg:
            print("Nothing to do. Use --list first, then --ids <id1,id2> [--apply].")
            return
        try:
            ids = [int(x) for x in ids_arg.replace(" ", "").split(",") if x]
        except ValueError:
            print("--ids must be comma-separated integers, e.g. --ids 12,15")
            return

        items = db.execute(select(NeedsReview).where(NeedsReview.id.in_(ids))).scalars().all()
        found = {it.id for it in items}
        for missing in [i for i in ids if i not in found]:
            print(f"  (no NeedsReview row with id={missing})")

        # Artifact states that mean a file is already in a FINAL Drive folder (not just staged in
        # Needs Review). Reprocessing such an item could duplicate/re-move the Drive file, so we
        # refuse -- only genuinely-unfiled pending items are eligible.
        FILED_ARTIFACT_STATES = {"filed", "duplicate"}

        # Show plan.
        plan = []
        for it in items:
            e = db.get(ProcessedEmail, it.email_id)
            if not e:
                print(f"  review id={it.id}: no ProcessedEmail -> skip")
                continue
            if e.gmail_message_id.startswith("drive-upload:"):
                print(f"  review id={it.id}: DRIVE UPLOAD (can't re-fetch from Gmail) -> skip. Re-drop the file instead.")
                continue
            if it.status != "pending":
                print(f"  review id={it.id}: status={it.status} (already resolved) -> skip")
                continue
            if e.status != "pending_review":
                print(f"  review id={it.id}: email status={e.status} (not cleanly pending_review) -> skip")
                continue
            arts = db.execute(select(FileArtifact).where(FileArtifact.email_id == e.id)).scalars().all()
            filed = [a for a in arts if a.status in FILED_ARTIFACT_STATES]
            if filed:
                print(f"  review id={it.id}: {len(filed)} attachment(s) already FILED to Drive -> SKIP "
                      f"(reprocessing could duplicate them; resolve this item in the dashboard instead).")
                continue
            plan.append((it, e))
            print(f"  review id={it.id} email_id={e.id} gmail={e.gmail_message_id[:16]}... :: {_safe(e.subject, 50)}")

        if not plan:
            print("\nNothing eligible to reprocess.")
            return
        if "--apply" not in sys.argv:
            print(f"\n[DRY RUN] Would reset + reprocess {len(plan)} item(s). Add --apply to run.")
            return

        # Reset + reprocess each, one at a time.
        from app.services.processing_service import ProcessingService
        processor = ProcessingService()
        for it, e in plan:
            gmail_id = e.gmail_message_id
            # Remove ONLY this email's old review row, staged artifacts, and logs so the run rebuilds
            # them fresh. Deliberately DO NOT delete ProcessedFile (the Drive dedup records) -- keeping
            # them means re-staging to the Needs Review folder is deduped, never duplicated in Drive.
            # Entities, other emails, and everything else are untouched.
            for a in db.execute(select(FileArtifact).where(FileArtifact.email_id == e.id)).scalars().all():
                db.delete(a)
            for l in db.execute(select(FilingLog).where(FilingLog.email_id == e.id)).scalars().all():
                db.delete(l)
            for r in db.execute(select(NeedsReview).where(NeedsReview.email_id == e.id)).scalars().all():
                db.delete(r)
            # Reset the email so process_message doesn't early-return on 'pending_review'.
            e.status = "new"
            e.last_error = None
            db.add(e)
            db.commit()
            try:
                result = processor.process_message(db, gmail_id)
                print(f"  review id={it.id} -> {result.get('status')}: {result.get('message')}")
            except Exception as exc:
                print(f"  review id={it.id} -> ERROR: {_safe(exc, 120)}")

        print("\nDone.")
    finally:
        db.close()


if __name__ == "__main__":
    main()

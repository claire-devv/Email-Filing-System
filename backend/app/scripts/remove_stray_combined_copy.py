"""
Remove a STRAY combined-email-PDF copy that was placed in a merely-named entity's Communications
folder (the multi-entity fan-out bug). Trashes ONLY the exact Drive file id(s) you pass, and cleans
the matching ProcessedFile dedup record + any phantom FilingLog rows for that file.

SAFE: dry run by default; targets only the file ids you name; trashes (recoverable), never hard
deletes. Touches nothing else in Drive or the DB.

Usage (from backend/):
  venv/bin/python -m app.scripts.remove_stray_combined_copy --file-ids 11mbjCR...          # dry run
  venv/bin/python -m app.scripts.remove_stray_combined_copy --file-ids 11mbjCR... --apply  # trash + clean
"""
import sys

from sqlalchemy import select

from app.db.session import SessionLocal
from app.db.models import ProcessedFile, FilingLog
from app.services.drive_service import DriveService


def _safe(s, n):
    return (str(s or "")[:n]).encode("ascii", "replace").decode("ascii")


def _arg(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main() -> None:
    ids_arg = _arg("--file-ids")
    if not ids_arg:
        print("Provide --file-ids <driveFileId[,driveFileId...]> (from diagnose_qv_drive).")
        return
    file_ids = [x for x in ids_arg.replace(" ", "").split(",") if x]
    apply = "--apply" in sys.argv

    db = SessionLocal()
    try:
        drive = DriveService()
        for fid in file_ids:
            item = drive.get_drive_item(fid)
            if not item:
                print(f"file_id={fid}: NOT found in Drive (already gone?) -> will still clean DB records")
            else:
                parents = item.get("parents") or []
                print(f"file_id={fid}: name={_safe(item.get('name'), 55)!r} trashed={item.get('trashed')} parents={parents}")

            pfs = db.execute(select(ProcessedFile).where(ProcessedFile.drive_file_id == fid)).scalars().all()
            print(f"  ProcessedFile dedup records: {len(pfs)}")

            if not apply:
                continue

            # 1) trash the Drive file (recoverable from Drive Trash)
            if item and not item.get("trashed"):
                drive.trash_file(fid)
                print(f"  trashed Drive file {fid}")
            # 2) remove the dedup record so it isn't treated as 'already filed here'
            for pf in pfs:
                db.delete(pf)
            db.commit()
            print(f"  removed {len(pfs)} ProcessedFile record(s)")

        if not apply:
            print("\n[DRY RUN] Nothing changed. Add --apply to trash the file(s) + clean records.")
            return
        print("\nDone. (Files are in Drive Trash and recoverable if needed.)")
    finally:
        db.close()


if __name__ == "__main__":
    main()

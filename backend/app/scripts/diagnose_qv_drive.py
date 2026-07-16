"""Read-only: check what's ACTUALLY in Drive for the Queen Village email — did the combined PDF
get copied into Queen Village's Communications folder (a real stray file to remove), or is it only
a phantom activity ROW with nothing extra in Drive?

Run on the server:  venv/bin/python -m app.scripts.diagnose_qv_drive
"""
from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import FileArtifact, ProcessedEmail, ProcessedFile, Entity
from app.services.drive_service import DriveService


def _safe(s, n):
    return (str(s or "")[:n]).encode("ascii", "replace").decode("ascii")


def _comm_folder_id(drive, entity_folder_id):
    q = (f"mimeType='application/vnd.google-apps.folder' and '{entity_folder_id}' in parents "
         f"and trashed=false and name='Communications'")
    resp = drive.service.files().list(
        q=q, fields="files(id,name)", supportsAllDrives=True,
        includeItemsFromAllDrives=True, corpora="allDrives",
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


db = SessionLocal()
try:
    drive = DriveService()
    e = db.execute(
        select(ProcessedEmail).where(ProcessedEmail.subject.ilike("%Queen Village open balances%"))
        .order_by(ProcessedEmail.id.desc())
    ).scalars().first()
    if not e:
        print("email not found")
        raise SystemExit

    aud = (e.metadata_json or {}).get("decision_audit") or {}
    involved = aud.get("auto_split_entities") or []
    print(f"email id={e.id}  auto_split_entities={involved}\n")

    combined = db.execute(
        select(FileArtifact).where(FileArtifact.email_id == e.id, FileArtifact.kind == "combined_package")
    ).scalars().first()
    if combined and combined.file_hash:
        pfs = db.execute(select(ProcessedFile).where(ProcessedFile.file_hash == combined.file_hash)).scalars().all()
        print(f"Combined email PDF is recorded as filed into {len(pfs)} Drive folder(s):")
        for pf in pfs:
            item = drive.get_drive_item(pf.drive_folder_id) or {}
            print(f"  folder_id={pf.drive_folder_id} name={_safe(item.get('name'), 30)!r} file={_safe(pf.filename, 50)!r}")
    else:
        print("no combined_package artifact/hash")

    print("\nPer-entity Communications check (does this email's PDF actually sit there?):")
    for name in involved:
        ent = db.execute(select(Entity).where(Entity.entity_name == name)).scalars().first()
        if not ent or not ent.drive_folder_id:
            print(f"  {name}: no entity folder")
            continue
        comm_id = _comm_folder_id(drive, ent.drive_folder_id)
        if not comm_id:
            print(f"  {name}: no Communications folder")
            continue
        files = drive.list_files_in_folder(comm_id)
        match = [f for f in files if "queen village" in (f.get("name") or "").lower()
                 or "open balance" in (f.get("name") or "").lower()]
        print(f"  {name} / Communications: {len(files)} files; matching THIS email: {len(match)}")
        for f in match:
            print(f"      -> {_safe(f.get('name'), 60)!r}  (id={f.get('id')})")
finally:
    db.close()

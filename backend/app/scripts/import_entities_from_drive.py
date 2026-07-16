"""Import the existing master client folders (Drive Level 1) into the entity registry.

The master client folders are the entity source of truth. Run this once after the client
adds the folders (and again whenever new master folders are added). Requires real Google:
    ENABLE_REAL_GOOGLE=true and DRIVE_ROOT_ID set.

Run:
    .\\.venv\\Scripts\\python.exe -m app.scripts.import_entities_from_drive
"""

from app.core.config import get_settings
from app.db.session import SessionLocal, init_db
from app.services.drive_service import DriveService
from app.services.entity_service import EntityService


def main() -> None:
    settings = get_settings()
    # Safe no-op when real Google is off (e.g. a scheduled task running in mock mode),
    # so the job never errors regardless of environment.
    if not settings.enable_real_google or not settings.drive_root_id:
        print({"skipped": "ENABLE_REAL_GOOGLE/DRIVE_ROOT_ID not configured"})
        return
    init_db()
    folders = DriveService().list_level1_folders()
    with SessionLocal() as db:
        result = EntityService().import_entities(db, folders)
    print(
        {
            "imported": result,
            "folders": [folder["name"] for folder in folders],
        }
    )


if __name__ == "__main__":
    main()

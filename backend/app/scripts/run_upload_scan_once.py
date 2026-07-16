"""
Run ONE Drive upload-folder scan on demand (no 15-minute loop, no server restart cycle).

Use this to live-test the "Client Uploads" / "RRES Uploads" feature: drop a file into an upload
folder, run this script, and watch it get classified, named, and moved -- or land in Needs Review.

Run from backend/ with the real .env in place:
    .venv\\Scripts\\python.exe -m app.scripts.run_upload_scan_once

Requirements (same as the email pipeline): ENABLE_REAL_GOOGLE=true, DRIVE_ROOT_ID set,
CLASSIFIER_MODE=claude + ENABLE_REAL_CLAUDE=true + ANTHROPIC_API_KEY. It temporarily forces the
scan ON for this run even if UPLOADS_SCAN_ENABLED is still false, so you can test before flipping
the flag in .env. It does a single pass of both _scan_uploads_once() and the upload-retry branch.
"""
import logging

from app.core.config import get_settings
from app.core.logging import configure_logging


def main() -> None:
    configure_logging()
    log = logging.getLogger("run_upload_scan_once")
    settings = get_settings()

    if not (settings.enable_real_google and settings.drive_root_id):
        log.error("ENABLE_REAL_GOOGLE=true and DRIVE_ROOT_ID are required. Aborting.")
        return

    # Force the scan on for this one-shot run regardless of the persisted flag, so it can be
    # tested before UPLOADS_SCAN_ENABLED is set in the server .env.
    if not settings.uploads_scan_enabled:
        object.__setattr__(settings, "uploads_scan_enabled", True)
        log.info("UPLOADS_SCAN_ENABLED was false; forcing it ON for this one-shot run only.")

    # Imported here so the settings override above is in effect when the loop reads it.
    from app.main import _retry_failed_uploads_once, _scan_uploads_once

    log.info("Scanning Client Uploads + RRES Uploads folders once ...")
    _scan_uploads_once()
    log.info("Retrying any previously-failed / API-limited uploads once ...")
    _retry_failed_uploads_once()
    log.info("Done. Check the dashboard Activity feed + Needs Review, and the Drive folders.")


if __name__ == "__main__":
    main()

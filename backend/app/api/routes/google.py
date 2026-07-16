from __future__ import annotations

import json
import os
from pathlib import Path
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import ENV_FILE, get_settings
from app.db.session import get_db
from app.services.drive_service import DriveService
from app.services.entity_service import EntityService
from app.services.google_auth import (
    build_google_auth_url,
    clear_credentials,
    google_auth_status,
)

router = APIRouter(prefix="/auth", tags=["google"])


class DriveRootUpdate(BaseModel):
    folder_url_or_id: str
    drive_root_name: str | None = None
    validate_access: bool = True


class NonEntityFoldersUpdate(BaseModel):
    folders: list[str]


@router.get("/google/status")
def google_status() -> dict:
    try:
        status = google_auth_status(include_profile=True)
    except Exception as exc:
        status = {
            "connected": False,
            "valid": False,
            "requires_reconnect": True,
            "status_error": str(exc),
        }
    settings = get_settings()
    return {
        **status,
        "drive_root_configured": bool(settings.drive_root_id),
        "drive_root_id": settings.drive_root_id,
        "drive_root_name": settings.drive_root_name,
        "redirect_uri": settings.google_oauth_redirect_uri,
    }


@router.get("/google/drive-root/status")
def google_drive_root_status() -> dict:
    settings = get_settings()
    payload = {
        "configured": bool(settings.drive_root_id),
        "drive_root_id": settings.drive_root_id,
        "drive_root_name": settings.drive_root_name,
        "accessible": False,
        "folder": None,
    }
    if not settings.drive_root_id:
        return payload
    try:
        item = DriveService().get_drive_item(settings.drive_root_id)
        payload["accessible"] = bool(item and not item.get("trashed"))
        payload["folder"] = item
    except Exception as exc:
        payload["access_error"] = str(exc)
    return payload


@router.put("/google/drive-root")
def google_drive_root_update(payload: DriveRootUpdate, db: Session = Depends(get_db)) -> dict:
    folder_id = _extract_drive_folder_id(payload.folder_url_or_id)
    if not folder_id:
        raise HTTPException(status_code=422, detail="Provide a Google Drive folder URL or folder ID.")

    folder_name = payload.drive_root_name
    item = None
    if payload.validate_access:
        try:
            item = DriveService().get_drive_item(folder_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not access Drive folder: {exc}") from exc
        if not item or item.get("trashed"):
            raise HTTPException(status_code=404, detail="Drive folder was not found or is trashed.")
        if item.get("mimeType") != "application/vnd.google-apps.folder":
            raise HTTPException(status_code=422, detail="Drive root must be a Google Drive folder.")
        folder_name = folder_name or item.get("name")

    _update_env_values(
        {
            "DRIVE_ROOT_ID": folder_id,
            "DRIVE_ROOT_NAME": folder_name or get_settings().drive_root_name,
        }
    )
    get_settings.cache_clear()
    settings = get_settings()

    entity_import: dict | None = None
    if payload.validate_access:
        try:
            folders = DriveService().list_level1_folders()
            entity_import = EntityService().import_entities(db, folders)
        except Exception as exc:
            entity_import = {"error": str(exc)}

    return {
        "configured": True,
        "drive_root_id": settings.drive_root_id,
        "drive_root_name": settings.drive_root_name,
        "accessible": bool(item and not item.get("trashed")) if payload.validate_access else None,
        "folder": item,
        "entity_import": entity_import,
    }


@router.get("/google/drive-root/folders")
def google_drive_root_folders() -> dict:
    # Every top-level folder under the Drive root, each flagged with whether it is currently
    # skipped (treated as a non-entity/noise folder). Powers the Settings folder-skip picker.
    settings = get_settings()
    if not settings.drive_root_id:
        return {"folders": [], "skip_list": settings.drive_non_entity_folders}
    skipped = {name.strip().lower() for name in (settings.drive_non_entity_folders or []) if name}
    try:
        folders = DriveService().list_root_folders()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not list Drive folders: {exc}") from exc
    return {
        "folders": [
            {"id": f["id"], "name": f["name"], "skipped": (f.get("name") or "").strip().lower() in skipped}
            for f in folders
        ],
        "skip_list": settings.drive_non_entity_folders,
    }


@router.put("/google/non-entity-folders")
def google_non_entity_folders_update(payload: NonEntityFoldersUpdate, db: Session = Depends(get_db)) -> dict:
    # Persist the reviewer's choice of which top-level folders are NOT client entities, then
    # re-import so deselected folders become entities and newly-skipped ones deactivate.
    cleaned = sorted({folder.strip() for folder in payload.folders if folder and folder.strip()}, key=str.lower)
    _update_env_values({"DRIVE_NON_ENTITY_FOLDERS": json.dumps(cleaned, ensure_ascii=False)})
    get_settings.cache_clear()

    entity_import: dict | None = None
    if get_settings().drive_root_id:
        try:
            folders = DriveService().list_level1_folders()
            entity_import = EntityService().import_entities(db, folders)
        except Exception as exc:
            entity_import = {"error": str(exc)}
    return {"skip_list": get_settings().drive_non_entity_folders, "entity_import": entity_import}


@router.get("/google/connect")
def google_connect(redirect: bool = Query(default=False)):
    try:
        payload = build_google_auth_url()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if redirect:
        return RedirectResponse(payload["auth_url"])
    return payload


@router.post("/google/disconnect")
def google_disconnect() -> dict:
    removed = clear_credentials()
    return {"connected": False, "token_removed": removed}


def _extract_drive_folder_id(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    for pattern in [
        r"/folders/([^/?#]+)",
        r"[?&]id=([^&#]+)",
    ]:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    if re.match(r"^[A-Za-z0-9_-]{10,}$", value):
        return value
    return None


def _update_env_values(updates: dict[str, str]) -> None:
    path = Path(ENV_FILE)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    for key, value in remaining.items():
        output.append(f"{key}={value}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    # Also update the live process environment. pydantic-settings reads os.environ with HIGHER
    # precedence than the .env file, and load_dotenv() seeded os.environ at startup -- so without
    # this, a freshly built Settings() would keep serving the OLD value and the drive root would
    # appear to "revert" right after saving. get_settings.cache_clear() then picks these up.
    for key, value in updates.items():
        os.environ[key] = value

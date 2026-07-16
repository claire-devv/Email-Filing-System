import io
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Entity, ProcessedFile
from app.services.google_auth import get_user_credentials
from app.services.rulebook_service import RulebookService

# Google's client retries 5xx, 429 and 403 rateLimitExceeded/userRateLimitExceeded with
# exponential backoff when num_retries is passed to .execute(num_retries=_DRIVE_NUM_RETRIES). A burst of concurrent
# filings otherwise trips "User rate limit exceeded" and fails the email.
_DRIVE_NUM_RETRIES = 5


class DriveService:
    # Entity folder ids whose full fixed Level-2 set has already been verified this process,
    # so repeat filings to the same entity don't re-list its children every time.
    _level2_checked: set[str] = set()

    def __init__(self) -> None:
        self.settings = get_settings()
        self.rulebook = RulebookService()
        self._service = None

    @property
    def service(self):
        if not self.settings.enable_real_google:
            raise RuntimeError("ENABLE_REAL_GOOGLE=true is required before calling Gmail/Drive APIs.")
        if self._service is None:
            self._service = build("drive", "v3", credentials=get_user_credentials(), cache_discovery=False)
        return self._service

    def ensure_root_ready(self) -> str:
        if not self.settings.drive_root_id:
            raise RuntimeError("DRIVE_ROOT_ID is required before filing to Google Drive.")
        return self.settings.drive_root_id

    def get_drive_item(self, file_id: str) -> dict | None:
        try:
            return self.service.files().get(
                fileId=file_id,
                fields="id,name,mimeType,trashed,parents,webViewLink,size",
                supportsAllDrives=True,
            ).execute(num_retries=_DRIVE_NUM_RETRIES)
        except HttpError as exc:
            if getattr(exc.resp, "status", None) in {404, 410}:
                return self.get_shared_drive_root(file_id)
            raise

    def get_shared_drive_root(self, drive_id: str) -> dict | None:
        try:
            drive = self.service.drives().get(
                driveId=drive_id,
                fields="id,name,hidden",
            ).execute(num_retries=_DRIVE_NUM_RETRIES)
        except HttpError as exc:
            if getattr(exc.resp, "status", None) in {404, 410}:
                return None
            raise
        return {
            "id": drive["id"],
            "name": drive.get("name"),
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": bool(drive.get("hidden")),
            "parents": [],
            "webViewLink": f"https://drive.google.com/drive/folders/{drive['id']}",
            "driveType": "shared_drive",
        }

    def download_file_stream(self, file_id: str, chunk_size: int = 8 * 1024 * 1024):
        # Generator of file bytes from Drive. Each chunk is a separate round-trip to Google's
        # servers, so a small chunk size multiplies request overhead for large files (an 18MB
        # file at the old 1MB chunk size was ~18 sequential round-trips). 8MB cuts that to ~2-3
        # round-trips while still bounding peak memory per chunk. Callers should probe
        # get_drive_item() first for a clean 404.
        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request, chunksize=chunk_size)
        done = False
        while not done:
            # Retry transient chunk failures (network blips, brief token refresh hiccups) instead
            # of aborting mid-stream, which otherwise sends the client a truncated file that the
            # browser can silently accept as a "successful" (but corrupt/unopenable) download.
            _, done = downloader.next_chunk(num_retries=_DRIVE_NUM_RETRIES)
            buffer.seek(0)
            yield buffer.read()
            buffer.seek(0)
            buffer.truncate(0)

    def folder_is_available(self, folder_id: str | None) -> bool:
        if not folder_id:
            return False
        item = self.get_drive_item(folder_id)
        return bool(
            item
            and not item.get("trashed")
            and item.get("mimeType") == "application/vnd.google-apps.folder"
        )

    def file_is_available_in_folder(self, file_id: str | None, folder_id: str | None) -> bool:
        if not file_id or not folder_id:
            return False
        item = self.get_drive_item(file_id)
        return bool(item and not item.get("trashed") and folder_id in (item.get("parents") or []))

    def ensure_folder(self, name: str, parent_id: str) -> dict:
        query = (
            "mimeType='application/vnd.google-apps.folder' "
            f"and name='{name.replace(chr(39), chr(92) + chr(39))}' "
            f"and '{parent_id}' in parents and trashed=false"
        )
        response = self.service.files().list(
            q=query,
            fields="files(id,name,webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        ).execute(num_retries=_DRIVE_NUM_RETRIES)
        files = response.get("files", [])
        if files:
            return files[0]
        return self.service.files().create(
            body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute(num_retries=_DRIVE_NUM_RETRIES)

    def ensure_entity_folder(self, db: Session, entity_name: str) -> Entity:
        root_id = self.ensure_root_ready()
        entity = db.execute(select(Entity).where(Entity.entity_name == entity_name)).scalars().first()
        if entity and entity.drive_folder_id:
            if self.folder_is_available(entity.drive_folder_id):
                # Entity folder already exists -> ensure the full fixed Level-2 set is present
                # under THIS entity, creating only the missing ones. Scoped to the entity being
                # filed to, never every entity in the Drive.
                self._ensure_level2_folders(entity.drive_folder_id)
                return entity
            entity.drive_folder_id = None
            db.add(entity)
            db.flush()
        # Entity folder does not exist -> create it and the full fixed Level-2 set.
        folder = self.ensure_folder(entity_name, root_id)
        self._ensure_level2_folders(folder["id"], force=True)
        if not entity:
            entity = Entity(entity_name=entity_name, folder_name=entity_name, drive_folder_id=folder["id"])
            db.add(entity)
        else:
            entity.drive_folder_id = folder["id"]
        db.commit()
        db.refresh(entity)
        return entity

    def _ensure_level2_folders(self, parent_folder_id: str, *, force: bool = False) -> None:
        # Ensure every fixed Level-2 folder exists under ONE entity. Lists the entity's
        # children once and creates only the missing folders (not 18 separate lookups). The
        # per-process cache skips re-listing an entity already verified this run.
        if not force and parent_folder_id in DriveService._level2_checked:
            return
        existing = self._child_folder_names(parent_folder_id)
        for level2 in self.rulebook.allowed_level2():
            if level2.strip().lower() not in existing:
                self.ensure_folder(level2, parent_folder_id)
        DriveService._level2_checked.add(parent_folder_id)

    def _child_folder_names(self, parent_folder_id: str) -> set[str]:
        query = (
            "mimeType='application/vnd.google-apps.folder' "
            f"and '{parent_folder_id}' in parents and trashed=false"
        )
        names: set[str] = set()
        page_token: str | None = None
        while True:
            response = self.service.files().list(
                q=query,
                fields="nextPageToken, files(name)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives",
            ).execute(num_retries=_DRIVE_NUM_RETRIES)
            for item in response.get("files", []):
                names.add((item.get("name") or "").strip().lower())
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return names

    def find_valid_processed_file(
        self,
        db: Session,
        file_hash: str,
        drive_folder_id: str,
    ) -> ProcessedFile | None:
        existing = db.execute(
            select(ProcessedFile).where(
                ProcessedFile.file_hash == file_hash,
                ProcessedFile.drive_folder_id == drive_folder_id,
            )
        ).scalars().first()
        if not existing:
            return None
        if self.file_is_available_in_folder(existing.drive_file_id, drive_folder_id):
            return existing
        db.delete(existing)
        db.flush()
        return None

    def ensure_needs_review_folder(self) -> dict:
        return self.ensure_folder(self.rulebook.review_folder_name(), self.ensure_root_ready())

    def list_root_folders(self) -> list[dict]:
        # Every top-level folder under the Drive root (id + name), skipping only the operational
        # Needs Review folder. This is the raw list the Settings UI shows so the user can choose
        # which folders are client entities and which are noise to skip.
        root_id = self.ensure_root_ready()
        review_name = self.rulebook.review_folder_name().strip().lower()
        query = (
            "mimeType='application/vnd.google-apps.folder' "
            f"and '{root_id}' in parents and trashed=false"
        )
        # When the root is a Shared Drive, scope the search to that drive (corpora="drive" +
        # driveId) -- the reliable way to list a Shared Drive's top level. Fall back to
        # "allDrives" for a regular My Drive folder root.
        root = self.get_drive_item(root_id)
        list_kwargs: dict = {
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
            "pageSize": 1000,
        }
        if (root or {}).get("driveType") == "shared_drive":
            list_kwargs["corpora"] = "drive"
            list_kwargs["driveId"] = root_id
        else:
            list_kwargs["corpora"] = "allDrives"
        folders: list[dict] = []
        page_token: str | None = None
        while True:
            response = self.service.files().list(
                q=query,
                fields="nextPageToken, files(id,name)",
                pageToken=page_token,
                **list_kwargs,
            ).execute(num_retries=_DRIVE_NUM_RETRIES)
            for item in response.get("files", []):
                if (item.get("name") or "").strip().lower() == review_name:
                    continue
                folders.append({"id": item["id"], "name": item["name"]})
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return folders

    def list_level1_folders(self) -> list[dict]:
        # The master client folders that become entities: the raw root folders minus any
        # configured non-entity/noise folders (e.g. "Unmatched", "RRES UPLOADS"), matched
        # case-insensitively.
        ignored = {name.strip().lower() for name in (self.settings.drive_non_entity_folders or []) if name}
        return [f for f in self.list_root_folders() if (f.get("name") or "").strip().lower() not in ignored]

    def list_files_in_folder(self, folder_id: str) -> list[dict]:
        # Every NON-folder file directly inside a folder (id, name, mimeType, size, createdTime,
        # parents). Used by the upload-folder scanner to find files a client/team dropped into a
        # "Client Uploads" / "RRES Uploads" folder. Mirrors _child_folder_names' pagination but
        # inverts the mimeType filter to return files, not folders.
        query = (
            "mimeType != 'application/vnd.google-apps.folder' "
            f"and '{folder_id}' in parents and trashed=false"
        )
        files: list[dict] = []
        page_token: str | None = None
        while True:
            response = self.service.files().list(
                q=query,
                fields="nextPageToken, files(id,name,mimeType,size,createdTime,parents,md5Checksum)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives",
            ).execute(num_retries=_DRIVE_NUM_RETRIES)
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return files

    def find_upload_folders(self) -> dict:
        # Discover the upload SOURCE folders in ONE/two drive-wide queries (cost is fixed, not per
        # entity): all folders literally named "Client Uploads" plus the single "RRES Uploads"
        # folder. Returns {"client_uploads": [{id, name, parents}, ...], "rres_uploads": {id,...}|None}.
        # The caller maps each Client Uploads folder to its owning entity via its parent id.
        root_id = self.ensure_root_ready()
        root = self.get_drive_item(root_id)
        list_kwargs: dict = {
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
            "pageSize": 1000,
        }
        if (root or {}).get("driveType") == "shared_drive":
            list_kwargs["corpora"] = "drive"
            list_kwargs["driveId"] = root_id
        else:
            list_kwargs["corpora"] = "allDrives"

        def _by_name(name: str) -> list[dict]:
            escaped = name.replace(chr(39), chr(92) + chr(39))
            query = (
                "mimeType='application/vnd.google-apps.folder' "
                f"and name='{escaped}' and trashed=false"
            )
            out: list[dict] = []
            page_token: str | None = None
            while True:
                response = self.service.files().list(
                    q=query,
                    fields="nextPageToken, files(id,name,parents)",
                    pageToken=page_token,
                    **list_kwargs,
                ).execute(num_retries=_DRIVE_NUM_RETRIES)
                out.extend(response.get("files", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return out

        client_uploads = _by_name(self.settings.client_uploads_folder_name)
        rres_candidates = _by_name(self.settings.rres_uploads_folder_name)
        # RRES Uploads must sit directly under the Drive root; ignore stray same-named folders.
        rres = next((f for f in rres_candidates if root_id in (f.get("parents") or [])), None)
        return {"client_uploads": client_uploads, "rres_uploads": rres}

    def resolve_target_folder(self, db: Session, entity_name: str, level2: str, level3: str | None) -> tuple[str, str]:
        level3 = self.rulebook.normalize_level3(level2, level3)
        entity = self.ensure_entity_folder(db, entity_name)
        level2_folder = self.ensure_folder(level2, entity.drive_folder_id)
        folder_id = level2_folder["id"]
        path = f"{self.settings.drive_root_name} / {entity_name} / {level2}"
        if level3:
            level3_folder = self.ensure_folder(level3, folder_id)
            folder_id = level3_folder["id"]
            path += f" / {level3}"
        return folder_id, path

    def upload_pdf_once(
        self,
        db: Session,
        local_path: Path,
        filename: str,
        drive_folder_id: str,
        file_hash: str,
        source_email_id: int | None,
    ) -> tuple[dict, bool]:
        existing = self.find_valid_processed_file(db, file_hash, drive_folder_id)
        if existing:
            item = self.get_drive_item(existing.drive_file_id) or {}
            if existing.filename != filename:
                item = self.rename_file(existing.drive_file_id, filename)
                existing.filename = filename
                db.add(existing)
                db.commit()
            return {
                "id": existing.drive_file_id,
                "name": existing.filename,
                "webViewLink": item.get("webViewLink") or f"https://drive.google.com/file/d/{existing.drive_file_id}/view",
            }, True
        media = MediaFileUpload(str(local_path), mimetype="application/pdf", resumable=local_path.stat().st_size > 100_000_000)
        created = self.service.files().create(
            body={"name": filename, "parents": [drive_folder_id]},
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute(num_retries=_DRIVE_NUM_RETRIES)
        db.add(
            ProcessedFile(
                file_hash=file_hash,
                drive_folder_id=drive_folder_id,
                drive_file_id=created["id"],
                filename=filename,
                source_email_id=source_email_id,
            )
        )
        db.commit()
        return created, False

    def move_file(self, file_id: str, from_folder_id: str, to_folder_id: str, new_name: str | None = None) -> dict:
        body = {"name": new_name} if new_name else None
        return self.service.files().update(
            fileId=file_id,
            body=body,
            addParents=to_folder_id,
            removeParents=from_folder_id,
            fields="id,name,webViewLink,parents",
            supportsAllDrives=True,
        ).execute(num_retries=_DRIVE_NUM_RETRIES)

    def rename_file(self, file_id: str, new_name: str) -> dict:
        return self.service.files().update(
            fileId=file_id,
            body={"name": new_name},
            fields="id,name,webViewLink,parents",
            supportsAllDrives=True,
        ).execute(num_retries=_DRIVE_NUM_RETRIES)

    def trash_file(self, file_id: str) -> dict:
        # Soft-delete (recoverable from Drive Trash). Used when an uploaded original is a
        # byte-identical duplicate of a doc already filed in its destination: we clear it out of
        # the upload folder without putting a second copy into the destination.
        return self.service.files().update(
            fileId=file_id,
            body={"trashed": True},
            fields="id,trashed",
            supportsAllDrives=True,
        ).execute(num_retries=_DRIVE_NUM_RETRIES)

    def convert_office_to_pdf(self, local_path: Path, google_mime_type: str, output_pdf: Path) -> None:
        media = MediaFileUpload(str(local_path), resumable=False)
        created = self.service.files().create(
            body={"name": local_path.stem, "mimeType": google_mime_type},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute(num_retries=_DRIVE_NUM_RETRIES)
        file_id = created["id"]
        try:
            request = self.service.files().export_media(fileId=file_id, mimeType="application/pdf")
            with output_pdf.open("wb") as handle:
                downloader = MediaIoBaseDownload(handle, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
        finally:
            self.service.files().delete(fileId=file_id, supportsAllDrives=True).execute(num_retries=_DRIVE_NUM_RETRIES)

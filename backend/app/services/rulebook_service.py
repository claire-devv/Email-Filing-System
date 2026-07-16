import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.models import FilingLog, FolderRulebook
from app.db.session import SessionLocal


class RulebookService:
    """Caches the folder-structure rulebook so processing does not read the file per email."""

    _cache: dict[str, Any] | None = None

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def path(self) -> Path:
        return self.settings.folder_rulebook_path

    def get(self) -> dict[str, Any]:
        if self.__class__._cache is None:
            self.reload_from_db_or_file()
        return deepcopy(self.__class__._cache or {})

    def allowed_level2(self) -> list[str]:
        return [item["name"] for item in self.get().get("level_2_folders", [])]

    def level3_rules(self) -> dict[str, str]:
        return {item["name"]: item.get("subfolder_rule", "none") for item in self.get().get("level_2_folders", [])}

    def subfolder_rule_for(self, level2: str | None) -> str:
        if not level2:
            return "none"
        return self.level3_rules().get(level2, "none")

    def normalize_level3(self, level2: str | None, level3: str | None) -> str | None:
        if not level3:
            return None
        if self.subfolder_rule_for(level2) == "none":
            return None
        return level3

    def review_folder_name(self) -> str:
        return self.get().get("review", {}).get("folder_name") or self.settings.needs_review_folder_name

    def reload_from_db_or_file(self) -> dict[str, Any]:
        try:
            return self.reload_from_file()
        except Exception as exc:
            self._log_system_error(f"Folder rulebook source failed, using DB cache if available: {exc}")
            cached = self._load_latest_from_db()
            if cached:
                self.__class__._cache = cached
                return self.get()
            raise RuntimeError("Folder rulebook failed to load and no DB cache exists. Email processing cannot start.") from exc

    def reload_from_file_safely(self) -> dict[str, Any]:
        try:
            return self.reload_from_file()
        except Exception as exc:
            self._log_system_error(f"Folder rulebook reload failed; keeping existing cache: {exc}")
            if self.__class__._cache:
                return self.get()
            cached = self._load_latest_from_db()
            if cached:
                self.__class__._cache = cached
                return self.get()
            raise

    def _load_latest_from_db(self) -> dict[str, Any] | None:
        with SessionLocal() as db:
            latest = db.execute(
                select(FolderRulebook).where(FolderRulebook.active.is_(True)).order_by(FolderRulebook.created_at.desc())
            ).scalars().first()
            if latest:
                return latest.rules_json
        return None

    def reload_from_file(self) -> dict[str, Any]:
        rules = json.loads(self.path.read_text(encoding="utf-8"))
        self._validate(rules)
        with SessionLocal() as db:
            db.execute(update(FolderRulebook).values(active=False))
            existing = db.execute(
                select(FolderRulebook).where(FolderRulebook.version == rules["version"])
            ).scalars().first()
            if existing:
                existing.source_path = str(self.path)
                existing.rules_json = rules
                existing.active = True
            else:
                db.add(FolderRulebook(version=rules["version"], source_path=str(self.path), rules_json=rules, active=True))
            db.commit()
        self.__class__._cache = rules
        return self.get()

    def update_file_and_cache(self, rules: dict[str, Any]) -> dict[str, Any]:
        self._validate(rules)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rules, indent=2), encoding="utf-8")
        return self.reload_from_file()

    @staticmethod
    def _validate(rules: dict[str, Any]) -> None:
        if not rules.get("version"):
            raise ValueError("Folder rulebook must include version.")
        if not rules.get("level_2_folders"):
            raise ValueError("Folder rulebook must include level_2_folders.")
        names = [item.get("name") for item in rules["level_2_folders"]]
        if len(names) != len(set(names)):
            raise ValueError("Folder rulebook contains duplicate Level 2 folder names.")
        if "Needs Review" in names:
            raise ValueError("Needs Review must be an operational review folder, not a client Level 2 folder.")

    def _log_system_error(self, message: str) -> None:
        try:
            with SessionLocal() as db:
                db.add(FilingLog(status="system_error", message=message))
                db.commit()
        except Exception:
            pass

import os
from pathlib import Path


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_artifact_path(path: str | None) -> Path | None:
    # Stored artifact paths may be absolute paths from before the repo restructure; if the
    # file isn't there, re-root the part after .../artifacts/ onto the configured artifact_root.
    if not path:
        return None
    if os.path.exists(path):
        return Path(path)
    normalized = path.replace("\\", "/")
    marker = "/artifacts/"
    if marker in normalized:
        from app.core.config import get_settings

        relative = normalized.split(marker, 1)[1]
        candidate = get_settings().artifact_root_resolved / relative
        if candidate.exists():
            return candidate
    return None


def safe_filename(value: str, fallback: str = "document") -> str:
    forbidden = '<>:"/\\|?*\n\r\t'
    cleaned = "".join("_" if ch in forbidden else ch for ch in value).strip(" .")
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    return cleaned[:180] or fallback


def relative_or_str(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)

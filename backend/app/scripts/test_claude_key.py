import hashlib
import os
from pathlib import Path

from anthropic import Anthropic

from app.core.config import get_settings


def _fingerprint(value: str | None) -> dict[str, str | int | bool | None]:
    if not value:
        return {"present": False, "prefix": None, "suffix": None, "length": 0, "sha256_12": None}
    return {
        "present": True,
        "prefix": value[:8],
        "suffix": value[-4:],
        "length": len(value),
        "sha256_12": hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper(),
    }


def _read_env_file_key() -> str | None:
    env_path = Path(".env")
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def main() -> None:
    settings = get_settings()
    key = settings.anthropic_api_key or ""
    env_file_key = _read_env_file_key()
    process_env_key = os.environ.get("ANTHROPIC_API_KEY")
    print(
        {
            "settings_key": _fingerprint(key),
            "env_file_key": _fingerprint(env_file_key),
            "process_env_key": _fingerprint(process_env_key),
            "model": settings.claude_model,
        }
    )
    if process_env_key and env_file_key and process_env_key != env_file_key:
        print("WARNING: Process ANTHROPIC_API_KEY is overriding .env. Remove it before starting the server.")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is empty in .env")
    if not settings.enable_real_claude:
        raise SystemExit("ENABLE_REAL_CLAUDE must be true to test the key")
    client = Anthropic(api_key=key)
    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=20,
        temperature=0,
        messages=[{"role": "user", "content": "Return only: ok"}],
    )
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    print({"status": "ok", "model": settings.claude_model, "response": text.strip()})


if __name__ == "__main__":
    main()

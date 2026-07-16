"""Backend test: token.json race-condition safety (google_auth.py).

Verifies the fix for the production `[Errno 2] ... token.json.tmp -> token.json` and
`Extra data` (corrupt JSON) errors: concurrent writes to the token file must never collide or
corrupt it, and a corrupt/unreadable token file must report a soft/transient status instead of
"needs reconnect". Nothing here touches the real Google APIs.

Run: python -m tests.test_google_auth_tokenfile
"""
import json
import os
import threading
from pathlib import Path

from app.core.config import get_settings
from app.services import google_auth


class _FakeCreds:
    def __init__(self, sub: str = "user"):
        self._sub = sub

    def to_json(self) -> str:
        return json.dumps({"token": f"tok-{self._sub}", "refresh_token": "rt", "client_id": "cid"})


def _use_temp_token_path(tmp_dir: Path) -> None:
    os.environ["GOOGLE_TOKEN_FILE"] = str(tmp_dir / "token.json")


def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _use_temp_token_path(tmp_dir)
        token_path = get_settings().google_token_file

        # --- Concurrent save_credentials from many threads must never raise and must always
        # leave a valid, parseable token.json (this is the exact [Errno 2] collision scenario:
        # multiple writers racing on save_credentials at once). ---
        errors: list[Exception] = []

        def _write(i: int) -> None:
            try:
                google_auth.save_credentials(_FakeCreds(sub=str(i)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"save_credentials raised under concurrency: {errors}"
        assert token_path.exists(), "token.json missing after concurrent writes"
        # File must be valid, complete JSON -- not truncated/doubled ("Extra data").
        data = json.loads(token_path.read_text(encoding="utf-8"))
        assert data.get("refresh_token") == "rt"

        # No leftover .tmp files (each writer's temp file must have been consumed by
        # os.replace, and unique names mean none could collide with the next writer's).
        leftover_tmp = list(tmp_dir.glob("token.json.*.tmp"))
        assert not leftover_tmp, f"leftover temp files after concurrent writes: {leftover_tmp}"

        # --- A corrupted token.json must not crash google_auth_status, and must NOT report
        # requires_reconnect=True (that was the false "Google disconnected" symptom). ---
        token_path.write_text('{"token": "abc", "refresh_token": "rt"} GARBAGE_TRAILING_BYTES', encoding="utf-8")
        status = google_auth.google_auth_status(include_profile=False)
        assert status["requires_reconnect"] is False, status
        assert status.get("transient_error"), status

        # --- _read_credentials_from_file must return None (not raise) on corrupt JSON. ---
        assert google_auth._read_credentials_from_file(token_path) is None

        # --- load_saved_credentials must return None (not raise) on corrupt JSON when the
        # file doesn't warrant a refresh attempt. ---
        assert google_auth.load_saved_credentials(refresh=False) is None

        # Restore a valid file so a subsequent no-token-file case can be checked cleanly.
        token_path.unlink()
        status = google_auth.google_auth_status(include_profile=False)
        assert status["requires_reconnect"] is True
        assert status["token_file_exists"] is False

    print("test_google_auth_tokenfile: all assertions passed")


if __name__ == "__main__":
    main()

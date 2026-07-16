from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build

from app.core.config import get_settings

try:
    import fcntl
except ImportError:  # Windows / non-POSIX dev machines: no cross-process file lock available.
    fcntl = None


SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
]
STATE_TTL_MINUTES = 15
logger = logging.getLogger(__name__)

# Serializes token refresh + write within a process. The dashboard fires several status
# checks at once (Google + Drive + Gmail watch); without this they would each try to
# refresh and rewrite token.json simultaneously, which produced spurious "needs reconnect".
_refresh_lock = threading.Lock()


@contextlib.contextmanager
def _cross_process_lock():
    """Inter-process complement to `_refresh_lock`: multiple uvicorn workers are separate
    processes with separate threading.Locks, so without this they can still refresh+write
    token.json at the same moment. Uses a sibling `token.json.lock` file with `fcntl.flock`
    (POSIX/production). On platforms without fcntl (local Windows dev), this is a no-op --
    the threading.Lock still covers the single-process case."""
    if fcntl is None:
        yield
        return
    token_path = get_settings().google_token_file
    lock_path = token_path.with_name(token_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_credentials_from_file(token_path: Path) -> Credentials | None:
    """Wraps `Credentials.from_authorized_user_file` so a corrupt/torn read (e.g. another
    process was mid-write) doesn't crash the caller or masquerade as a revoked token. Returns
    None on a corrupt read -- callers treat that as "temporarily unreadable", not
    "disconnected", so a transient torn read self-heals on the next successful read."""
    try:
        return Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.warning(
            "Transient error reading %s (likely a concurrent write in progress): %s", token_path, exc
        )
        return None


def get_user_credentials(*, allow_interactive: bool = False) -> Credentials:
    settings = get_settings()
    token_path = settings.google_token_file
    secret_path = settings.google_client_secret_file
    creds = load_saved_credentials(refresh=True)
    if not creds or not creds.valid:
        if not allow_interactive:
            raise RuntimeError("Google account is not connected. Use /auth/google/connect first.")
        if not secret_path.exists():
            raise RuntimeError(f"Missing Google OAuth client secret: {secret_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
        creds = flow.run_local_server(port=0)
    save_credentials(creds)
    return creds


def load_saved_credentials(*, refresh: bool = False) -> Credentials | None:
    settings = get_settings()
    token_path = settings.google_token_file
    if not token_path.exists():
        return None
    creds = _read_credentials_from_file(token_path)
    if creds is None:
        return None
    if refresh and creds.expired and creds.refresh_token:
        with _refresh_lock, _cross_process_lock():
            # Re-read inside the lock: a concurrent request/process may have just refreshed
            # and rewritten the token file, in which case there is nothing left to do.
            refreshed = _read_credentials_from_file(token_path)
            if refreshed is not None:
                creds = refreshed
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                save_credentials(creds)
    return creds


def save_credentials(creds: Credentials) -> None:
    token_path = get_settings().google_token_file
    token_path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a reader never sees a half-written token.json (a torn read was
    # another source of false "needs reconnect"). os.replace is atomic on the same volume. The
    # temp filename is unique per call (pid + random) so two concurrent writers never collide
    # on the same temp path -- a shared name meant one writer's os.replace could find the
    # other's temp file already renamed away ([Errno 2]).
    tmp_path = token_path.with_name(f"{token_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        tmp_path.write_text(creds.to_json(), encoding="utf-8")
        # os.replace is atomic on POSIX. On Windows it can transiently raise PermissionError
        # if another process/AV briefly has the destination open; a couple of retries clears
        # that without weakening the atomicity guarantee (each retry is still a single
        # all-or-nothing replace, never a partial write).
        last_exc: OSError | None = None
        for attempt in range(20):
            try:
                os.replace(tmp_path, token_path)
                last_exc = None
                break
            except PermissionError as exc:
                last_exc = exc
                time.sleep(0.02 * (attempt + 1))
        if last_exc is not None:
            raise last_exc
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def clear_credentials() -> bool:
    token_path = get_settings().google_token_file
    if token_path.exists():
        token_path.unlink()
        return True
    return False


def google_auth_status(*, include_profile: bool = True) -> dict[str, Any]:
    token_path = get_settings().google_token_file
    if not token_path.exists():
        # No saved token at all -> the account has never been connected (or was disconnected).
        return {
            "connected": False,
            "token_file_exists": False,
            "email": None,
            "scopes": [],
            "expired": None,
            "valid": False,
            "has_refresh_token": False,
            "expiry": None,
            "requires_reconnect": True,
        }

    creds = _read_credentials_from_file(token_path)
    if creds is None:
        # A concurrent writer left the file momentarily unreadable. Not a real disconnect --
        # report a soft/transient state so the UI doesn't flash "connect Google account".
        return {
            "connected": True,
            "token_file_exists": True,
            "email": None,
            "scopes": [],
            "expired": None,
            "valid": False,
            "has_refresh_token": True,
            "expiry": None,
            "requires_reconnect": False,
            "transient_error": "Token file is temporarily unreadable (concurrent write in progress).",
        }
    has_refresh_token = bool(creds.refresh_token)
    transient_error: str | None = None
    if creds.expired and creds.refresh_token:
        try:
            creds = load_saved_credentials(refresh=True) or creds
        except RefreshError as exc:
            # The refresh token itself was rejected (revoked / expired / scope change). This
            # is the ONLY refresh outcome that truly needs the user to reconnect.
            return {
                "connected": False,
                "token_file_exists": True,
                "email": None,
                "scopes": list(creds.scopes or []),
                "expired": True,
                "valid": False,
                "has_refresh_token": has_refresh_token,
                "expiry": creds.expiry.isoformat() if creds.expiry else None,
                "requires_reconnect": True,
                "status_error": str(exc),
            }
        except Exception as exc:
            # Transient failure (network blip, Google 5xx). The stored refresh token is still
            # good, so do NOT raise a reconnect alarm — report a soft, non-blocking error and
            # treat the account as connected.
            transient_error = str(exc)

    valid = bool(creds.valid)
    status = {
        # A transient refresh error still counts as connected as long as we hold a refresh token.
        "connected": valid or (has_refresh_token and transient_error is not None),
        "token_file_exists": True,
        "email": None,
        "scopes": list(creds.scopes or []),
        "expired": bool(creds.expired),
        "valid": valid,
        "has_refresh_token": has_refresh_token,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
        # Only prompt the user to reconnect when there is genuinely no usable refresh token.
        "requires_reconnect": not has_refresh_token and not valid,
    }
    if transient_error:
        status["transient_error"] = transient_error
    if valid and include_profile:
        try:
            profile = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()
            status["email"] = profile.get("emailAddress")
        except Exception as exc:
            status["profile_error"] = str(exc)
    return status


def build_google_auth_url() -> dict[str, str]:
    settings = get_settings()
    if not settings.google_client_secret_file.exists():
        raise RuntimeError(f"Missing Google OAuth client secret: {settings.google_client_secret_file}")
    state = create_oauth_state()
    flow = Flow.from_client_secrets_file(
        str(settings.google_client_secret_file),
        scopes=SCOPES,
        redirect_uri=settings.google_oauth_redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    update_oauth_state(state, {"code_verifier": flow.code_verifier})
    return {
        "auth_url": auth_url,
        "state": state,
        "redirect_uri": settings.google_oauth_redirect_uri,
    }


def exchange_callback_code(*, code: str, state: str) -> Credentials:
    settings = get_settings()
    state_data = consume_oauth_state(state)
    flow = Flow.from_client_secrets_file(
        str(settings.google_client_secret_file),
        scopes=SCOPES,
        redirect_uri=settings.google_oauth_redirect_uri,
        code_verifier=state_data.get("code_verifier"),
    )
    flow.fetch_token(code=code)
    if not flow.credentials:
        raise RuntimeError("Google OAuth callback did not return credentials.")
    save_credentials(flow.credentials)
    return flow.credentials


def create_oauth_state() -> str:
    settings = get_settings()
    state = secrets.token_urlsafe(32)
    states = _load_states(settings.google_oauth_state_file)
    now = datetime.now(timezone.utc)
    states = {
        key: value
        for key, value in states.items()
        if _parse_datetime(value.get("created_at")) > now - timedelta(minutes=STATE_TTL_MINUTES)
    }
    states[state] = {
        "created_at": now.isoformat(),
        "redirect_uri": settings.google_oauth_redirect_uri,
    }
    _save_states(settings.google_oauth_state_file, states)
    return state


def update_oauth_state(state: str, updates: dict[str, Any]) -> None:
    settings = get_settings()
    states = _load_states(settings.google_oauth_state_file)
    if state not in states:
        raise RuntimeError("OAuth state expired before auth URL was built.")
    states[state] = {**states[state], **updates}
    _save_states(settings.google_oauth_state_file, states)


def consume_oauth_state(state: str) -> dict[str, Any]:
    settings = get_settings()
    states = _load_states(settings.google_oauth_state_file)
    data = states.pop(state, None)
    _save_states(settings.google_oauth_state_file, states)
    if not data:
        raise RuntimeError("Invalid or expired Google OAuth state.")
    created_at = _parse_datetime(data.get("created_at"))
    if created_at < datetime.now(timezone.utc) - timedelta(minutes=STATE_TTL_MINUTES):
        raise RuntimeError("Expired Google OAuth state.")
    return data


def _load_states(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_states(path: Path, states: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(states, indent=2), encoding="utf-8")


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

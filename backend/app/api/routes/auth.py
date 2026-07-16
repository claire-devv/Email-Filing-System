from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import create_access_token, verify_credentials
from app.db.session import get_db
from app.services.google_auth import exchange_callback_code, google_auth_status

router = APIRouter(prefix="/auth", tags=["auth"])


class DashboardLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
def dashboard_login(payload: DashboardLoginRequest, db: Session = Depends(get_db)) -> dict:
    # Public endpoint: validates against the dashboard_users table and returns a
    # bearer token used to access the protected dashboard API routes.
    settings = get_settings()
    if not settings.auth_jwt_secret:
        raise HTTPException(
            status_code=503,
            detail="Dashboard auth is not configured. Set AUTH_JWT_SECRET.",
        )
    user = verify_credentials(db, payload.email, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return create_access_token(user)


@router.get("/google/callback", response_class=HTMLResponse)
def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    settings = get_settings()
    if error:
        return _finish_callback(
            success=False,
            title="Google connection cancelled",
            message=error_description or error,
            redirect_url=settings.google_oauth_error_redirect_url,
        )
    if not code or not state:
        return _finish_callback(
            success=False,
            title="Google connection failed",
            message="The Google callback was missing code or state.",
            redirect_url=settings.google_oauth_error_redirect_url,
        )
    try:
        exchange_callback_code(code=code, state=state)
        status = google_auth_status(include_profile=True)
    except Exception as exc:
        return _finish_callback(
            success=False,
            title="Google connection failed",
            message=str(exc),
            redirect_url=settings.google_oauth_error_redirect_url,
        )
    email = status.get("email") or "Google account"
    return _finish_callback(
        success=True,
        title="Google connected",
        message=f"{email} is now connected for Gmail and Drive filing.",
        redirect_url=settings.google_oauth_success_redirect_url,
        query={"connected": "true", "email": email},
    )


def _finish_callback(
    *,
    success: bool,
    title: str,
    message: str,
    redirect_url: str | None,
    query: dict[str, str] | None = None,
):
    if redirect_url:
        separator = "&" if "?" in redirect_url else "?"
        suffix = urlencode(query or {"connected": str(success).lower(), "message": message})
        return RedirectResponse(f"{redirect_url}{separator}{suffix}")
    color = "#137333" if success else "#b3261e"
    return HTMLResponse(
        f"""
        <!doctype html>
        <html>
          <head>
            <meta charset="utf-8" />
            <title>{title}</title>
            <style>
              body {{ font-family: Arial, sans-serif; margin: 48px; color: #202124; }}
              .status {{ color: {color}; font-size: 20px; font-weight: 700; margin-bottom: 12px; }}
              .box {{ max-width: 720px; border: 1px solid #dadce0; border-radius: 8px; padding: 24px; }}
            </style>
          </head>
          <body>
            <div class="box">
              <div class="status">{escape(title)}</div>
              <p>{escape(message)}</p>
              <p>You can close this tab and return to the RRES dashboard.</p>
            </div>
          </body>
        </html>
        """
    )

"""Shared error classification + human-readable messages for the filing pipelines.

Used by both the email pipeline (processing_service) and the Drive-upload pipeline
(upload_ingest_service) so a password-protected/corrupt document produces the SAME clean message
and the SAME "don't churn retries" behaviour regardless of source.
"""
import re

import anthropic
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError

# Substrings that mark a PERMANENT, unretryable problem with the document itself (retrying will
# always fail the same way). These should route to Needs Review, not the retry loop.
_PERMANENT_ERROR_MARKERS = (
    "password protected",
    "password-protected",
    "encrypted",
    "pdf is invalid",
    "invalid pdf",
    "could not process",
    "unsupported file",
    "unreadable",
)


def is_permanent_error(exc: Exception) -> bool:
    """True when the error is inherent to the file (password/corrupt/unsupported) so retrying is
    pointless."""
    msg = str(exc).lower()
    return any(m in msg for m in _PERMANENT_ERROR_MARKERS)


# Substrings marking an ACCOUNT-level billing problem. Used to tell "credit balance too low" (400
# BadRequestError, account-wide, will resolve once billing is fixed) apart from other 400s like an
# oversized/malformed prompt (a genuine request bug -- retrying an unchanged request fails the
# same way forever, so it must NOT be treated as retry-worthy).
_BILLING_ERROR_MARKERS = (
    "credit balance is too low",
    "insufficient_quota",
)


def is_billing_error(exc: Exception) -> bool:
    """True when the error text indicates the account itself is out of credits/billing-blocked."""
    msg = str(exc).lower()
    return any(m in msg for m in _BILLING_ERROR_MARKERS)


# Anthropic SDK exception types that mean CLAUDE ITSELF is temporarily unreachable, rate-limited,
# overloaded, or the account's access is blocked -- as opposed to a problem with this specific
# request's content (e.g. a too-large prompt, which is also a BadRequestError but will fail the
# same way no matter how many times it's retried). Covers exactly what a client would describe as
# "AI model access is down for any reason": expired/out-of-quota tokens, Anthropic maintenance,
# Anthropic server errors, rate limits, network issues reaching the API, and an invalid/revoked key.
_RETRYABLE_API_ERROR_TYPES = (
    anthropic.RateLimitError,        # 429 -- rate limited
    anthropic.InternalServerError,   # 5xx -- Anthropic server errors / overloaded / maintenance
    anthropic.APIConnectionError,    # network/timeout reaching Anthropic at all (covers APITimeoutError)
    anthropic.AuthenticationError,   # API key invalid/expired -- access blocked account-wide
    anthropic.PermissionDeniedError,  # API key lacks permission -- access blocked account-wide
)


# HTTP status codes from Gmail/Drive (googleapiclient.errors.HttpError) that mean the Google API
# itself is transiently unavailable -- rate limited, overloaded, or erroring server-side -- rather
# than anything wrong with this specific request. 400/403/404 are deliberately excluded: those
# usually mean a genuine, structural problem (bad request, revoked/insufficient permission, file
# gone) that an identical retry will not fix.
_RETRYABLE_GOOGLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _google_http_status(exc: HttpError) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


def is_api_unavailable_error(exc: Exception) -> bool:
    """True when an external API this app depends on (Claude, Gmail, or Drive) is temporarily
    unreachable/blocked -- rate limit, server error or maintenance, network/connection issue, an
    expired/invalid/revoked credential, or a billing/credit block -- rather than a problem with
    this specific email's content.

    This should never count toward the "give up on this email" attempt ceiling: waiting and
    retrying is GUARANTEED to eventually succeed once the provider recovers / the rate limit
    clears / billing is fixed / the credential is restored, because nothing about the email itself
    needs to change. Contrast with is_permanent_error (a broken file, retrying never helps) and an
    ordinary BadRequestError/400 caused by a bug in our own request (retrying an unchanged request
    never helps either, so those correctly keep the normal finite-attempt ceiling).

    Covers Claude AND Gmail/Drive because a client-facing "AI model access is down for any reason"
    guarantee only holds if EVERY external dependency in the filing pipeline degrades the same way
    -- a Gmail/Drive outage should self-heal exactly like a Claude outage does, not silently
    exhaust the retry ceiling the way the original credits bug did.
    """
    if isinstance(exc, _RETRYABLE_API_ERROR_TYPES):
        return True
    if isinstance(exc, anthropic.BadRequestError):
        return is_billing_error(exc)
    if isinstance(exc, HttpError) and _google_http_status(exc) in _RETRYABLE_GOOGLE_HTTP_STATUSES:
        return True
    # Google OAuth/credentials broken or unreachable -- e.g. an invalid/expired client secret
    # (google.auth.exceptions.RefreshError), exactly the failure mode that broke large-file Drive
    # downloads earlier this session. Once a human fixes the credential, this self-heals too.
    if isinstance(exc, GoogleAuthError):
        return True
    return False


def api_unavailable_reason(exc: Exception) -> str:
    """Short tag for metadata/diagnostics explaining WHY the API was treated as unavailable."""
    if isinstance(exc, anthropic.RateLimitError):
        return "claude_rate_limited"
    if isinstance(exc, anthropic.InternalServerError):
        return "claude_server_error"
    if isinstance(exc, anthropic.APIConnectionError):
        return "claude_connection_error"
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "claude_auth_error"
    if isinstance(exc, anthropic.BadRequestError) and is_billing_error(exc):
        return "billing"
    if isinstance(exc, HttpError):
        status = _google_http_status(exc)
        return "google_rate_limited" if status == 429 else "google_server_error"
    if isinstance(exc, GoogleAuthError):
        return "google_auth_error"
    return "api_unavailable"


def clean_error_message(exc: Exception) -> str:
    """Turn a raw provider error (e.g. the Anthropic 400 JSON blob) into a short human sentence for
    the dashboard, instead of dumping ``Error code: 400 - {'type': 'error', ...}``. Wording is
    source-neutral (works for both emailed and uploaded documents)."""
    raw = str(exc)
    low = raw.lower()
    if "password protected" in low or "password-protected" in low or "encrypted" in low:
        return "This PDF is password protected, so it can't be read. Provide an unlocked copy."
    if "invalid pdf" in low or "pdf is invalid" in low or "unreadable" in low:
        return "This PDF appears to be corrupt or unreadable. Provide a re-exported copy."
    if "too many pages" in low or ("maximum" in low and "pages" in low):
        return "This document has too many pages to process automatically. It needs manual filing."
    # Fallback: strip the noisy provider envelope if present, keep the human-readable 'message'.
    m = re.search(r"'message':\s*'([^']+)'", raw)
    if m:
        return m.group(1).strip()
    return raw[:300]

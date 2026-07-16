from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# Phase 1 runs locally, so the project .env should win over any stale shell
# variable left behind by prior testing.
load_dotenv(ENV_FILE, override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    app_name: str = "RRES Phase 1 Local Backend"
    database_url: str = "sqlite:///./runtime/rres_phase1.db"
    artifact_root: Path = Path("./runtime/artifacts")
    folder_rulebook_path: Path = Path("./data/folder_structure.json")

    # Business/display timezone. Day-based stats ("today") are bucketed on this zone so the
    # client's day boundary is midnight Eastern, not midnight UTC. The dashboard also renders
    # all timestamps in this zone. IANA name; handles EST/EDT automatically.
    display_timezone: str = "America/New_York"

    auto_file_confidence: int = Field(default=80, ge=0, le=100)
    urgent_review_confidence: int = Field(default=50, ge=0, le=100)
    # Max distinct entities a single email may auto-split across (attachments filed to their
    # own client folders, combined PDF copied to each). Above this the email is routed to
    # Needs Review for a human to split, so worst-case Drive operations per email stay bounded.
    max_auto_split_entities: int = Field(default=3, ge=1, le=10)
    max_file_size_mb: int = 500
    zip_max_depth: int = 3
    zip_max_extracted_mb: int = 500
    # Per-PDF caps for what is sent to Claude as an image document. A scanned PDF over
    # claude_pdf_max_pages is TRUNCATED to its first N pages (so Claude still reads it instead of
    # the API rejecting an over-100-page request); one over claude_pdf_max_mb is skipped entirely
    # (classified from its text preview).
    claude_pdf_max_mb: int = 32
    claude_pdf_max_pages: int = 100
    # Ceiling on the COMBINED size of all PDF image-documents attached to one classification call,
    # so an email with several large scans can't overflow Claude's context window. Once spent,
    # remaining scanned PDFs fall back to their text preview. Keep well under the model window.
    claude_pdf_total_max_mb: int = 24
    pubsub_max_delivery_attempts: int = 5
    # Max concurrent WeasyPrint email-body renders PER worker process. WeasyPrint is the
    # memory-heavy step; bounding it stops a burst of emails from all rendering at once
    # and exhausting RAM (OOM / swap-thrash). Effective box-wide cap = this x uvicorn workers.
    weasyprint_max_concurrent_renders: int = 3

    classifier_mode: str = "mock"
    anthropic_api_key: str | None = None
    claude_model: str = "claude-sonnet-4-6"
    enable_real_claude: bool = False
    claude_daily_call_limit: int = 20
    claude_max_calls_per_process: int = 1
    # Larger so the first few pages of each attachment reach Claude (account/policy numbers,
    # statement-period dates needed for Level 3 live there). Only a ceiling — most emails
    # stay far smaller; attachment-heavy ones get the headroom. Raised so a 10-attachment email
    # fits every attachment's preview (text tokens are cheap, well within the model window).
    claude_max_prompt_chars: int = 120000
    # Cap on just the per-attachment payload block inside the prompt. Sized so ~10 attachments at
    # 8000 chars each all reach Claude instead of only the first ~5 (the old 40000 cap).
    claude_artifact_payload_chars: int = 90000
    # Output token ceiling for the classification JSON. Emails with many attachments emit a
    # large artifact_summaries block; too low a ceiling truncates the JSON mid-response and
    # the parse fails (a 0%-confidence "Claude Classification Needs Review" item). Claude is
    # only billed for tokens actually emitted, so a generous ceiling is safe.
    claude_max_output_tokens: int = 4096

    # Dashboard login (JWT bearer auth for the React dashboard). All three must be set
    # to enable POST /auth/login and to protect the dashboard API routes; if any is
    # unset, login returns 503 and the protected routes reject every request.
    dashboard_auth_email: str | None = None
    dashboard_auth_password: str | None = None
    auth_jwt_secret: str | None = None
    auth_token_ttl_minutes: int = 720

    enable_real_google: bool = False
    # Local testing aid: when true, the background retry loop is disabled so emails parked as
    # "failed"/"waiting_api_limit" are NEVER auto-retried (no wasted Claude calls on a backlog).
    # New emails still process normally via the Pub/Sub webhook and "Run now". Default false =
    # normal production behavior (retries on). Safe to toggle; affects only the retry loop.
    process_new_only: bool = False
    process_unread_max_limit: int = 5
    # Max emails doing heavy Claude+Drive work concurrently (per worker). Caps the burst
    # when a Pub/Sub backlog flushes, so we don't trip Anthropic 5xx / Drive rate limits.
    process_max_concurrent: int = 3
    google_client_secret_file: Path = Path("./credentials/client_secret.json")
    google_token_file: Path = Path("./credentials/token.json")
    google_oauth_state_file: Path = Path("./runtime/google_oauth_states.json")
    google_oauth_redirect_uri: str = "http://127.0.0.1:8088/auth/google/callback"
    google_oauth_success_redirect_url: str | None = None
    google_oauth_error_redirect_url: str | None = None
    google_application_credentials: str | None = None
    gmail_user_id: str = "me"
    gmail_pubsub_topic_name: str | None = None
    # Gmail watches expire after ~7 days; the in-app loop re-watches on this cadence so
    # Pub/Sub push notifications never lapse. 0 disables the in-app auto-renewal.
    gmail_watch_renew_interval_hours: int = 24
    gmail_pubsub_label_ids: list[str] = ["INBOX"]
    gmail_pubsub_label_filter_behavior: str = "INCLUDE"
    gmail_filed_label: str = "RRES-Filed"
    gmail_failed_label: str = "RRES-Failed"
    gmail_skipped_label: str = "RRES-Skipped"

    # Internal relay inboxes that forward mail for every client (e.g. the RRES filing
    # address). Learned sender/domain mappings for these would steer all future
    # forwarded email toward one entity, so learning skips them.
    forwarder_domains: list[str] = []

    drive_root_id: str | None = None
    drive_root_name: str = "RRES - Books"
    needs_review_folder_name: str = "Needs Review"
    # Top-level Drive folders that are NOT client entities (operational/noise folders on the
    # real client Drive). They are skipped when importing entities from the Drive root so they
    # never become phantom entities. "Needs Review" is handled separately via its own setting.
    drive_non_entity_folders: list[str] = ["Unmatched", "RRES UPLOADS", "RRES Uploads"]
    upload_combined_package: bool = True
    admin_api_key: str | None = None

    # Drive upload-folder scanning: files clients/team drop into "Client Uploads" (per entity) or
    # "RRES Uploads" (one, at root) are read, named, and MOVED into the right folder like emails.
    # Off by default until those folders exist + are shared on the live Drive. The scanner runs as
    # a background loop every uploads_scan_interval_minutes. See backend/DRIVE_UPLOADS_PLAN.md.
    uploads_scan_enabled: bool = False
    uploads_scan_interval_minutes: int = 15
    client_uploads_folder_name: str = "Client Uploads"
    rres_uploads_folder_name: str = "RRES Uploads"

    # Include 5174: Vite falls back to it when 5173 is already taken.
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]

    @property
    def artifact_root_resolved(self) -> Path:
        return self.artifact_root.resolve()

    @property
    def sqlite_path(self) -> Path | None:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        value = self.database_url.removeprefix(prefix)
        if value.startswith("./"):
            return Path(value).resolve()
        return Path(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()

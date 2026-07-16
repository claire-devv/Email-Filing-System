from app.core.config import get_settings
from app.services.google_auth import get_user_credentials


def main() -> None:
    settings = get_settings()
    print("Starting Google OAuth setup.")
    print(f"Client secret: {settings.google_client_secret_file}")
    print(f"Token output:  {settings.google_token_file}")
    creds = get_user_credentials(allow_interactive=True)
    print("Google OAuth token saved.")
    print(f"Scopes: {', '.join(creds.scopes or [])}")


if __name__ == "__main__":
    main()

"""Create or promote a dashboard ADMIN login (e.g. to hand the system to the client).

The dashboard Users panel only creates non-admin users, and the env-based seed runs only
on an empty table -- so this script is the supported way to mint a new admin. You choose the
password (entered hidden, never stored in shell history or seen by anyone else); it is stored
only as a PBKDF2 hash.

Run from the backend/ directory (venv active):
    # create a brand-new admin (prompts for password):
    ./venv/bin/python -m app.scripts.create_admin_user --email client@example.com

    # promote/reactivate an existing user to admin (keeps their password):
    ./venv/bin/python -m app.scripts.create_admin_user --email someone@example.com

    # also reset an existing user's password:
    ./venv/bin/python -m app.scripts.create_admin_user --email someone@example.com --reset-password
"""

from __future__ import annotations

import argparse
import getpass

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import DashboardUser
from app.db.session import SessionLocal, init_db

MIN_PASSWORD_LEN = 8


def _prompt_password() -> str:
    first = getpass.getpass("New admin password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("Passwords did not match.")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or promote a dashboard admin user.")
    parser.add_argument("--email", required=True, help="Login email for the admin.")
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="If the user already exists, also set a new password (otherwise the existing one is kept).",
    )
    args = parser.parse_args()

    email = (args.email or "").strip().lower()
    if "@" not in email:
        raise SystemExit("Provide a valid email with --email.")

    init_db()
    with SessionLocal() as db:
        user = db.execute(select(DashboardUser).where(DashboardUser.email == email)).scalars().first()

        if user is None:
            password = _prompt_password()
            if len(password) < MIN_PASSWORD_LEN:
                raise SystemExit(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
            db.add(
                DashboardUser(
                    email=email,
                    password_hash=hash_password(password),
                    active=True,
                    is_admin=True,
                )
            )
            db.commit()
            print(f"Created admin user: {email}")
            return

        # Existing user: promote + reactivate, and optionally reset the password.
        changes: list[str] = []
        if not user.is_admin:
            user.is_admin = True
            changes.append("promoted to admin")
        if not user.active:
            user.active = True
            changes.append("reactivated")
        if args.reset_password:
            password = _prompt_password()
            if len(password) < MIN_PASSWORD_LEN:
                raise SystemExit(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
            user.password_hash = hash_password(password)
            changes.append("password reset")
        if not changes:
            print(f"User {email} is already an active admin. No changes.")
            return
        db.add(user)
        db.commit()
        print(f"Updated {email}: {', '.join(changes)}.")


if __name__ == "__main__":
    main()

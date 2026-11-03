"""One-off admin commands.

    python -m app.cli create-admin --email you@example.com --name "Rohit Singh"
    python -m app.cli test-push
"""

import argparse
import getpass
import sys

from sqlalchemy import func, select

from app.db import get_sessionmaker
from app.models import AdminUser, PushDevice
from app.services import push
from app.services.security import hash_password


def create_admin(email: str, name: str) -> None:
    password = getpass.getpass("Password (min 12 characters): ")
    if len(password) < 12:
        sys.exit("Password must be at least 12 characters.")
    if getpass.getpass("Repeat password: ") != password:
        sys.exit("Passwords do not match.")

    with get_sessionmaker()() as db:
        if db.scalar(select(AdminUser).where(AdminUser.email == email)):
            sys.exit(f"An admin with email {email} already exists.")
        db.add(AdminUser(email=email, name=name, password_hash=hash_password(password)))
        db.commit()
    print(f"Created admin {email}")


def test_push() -> None:
    """Send a test notification to every registered phone (checks FIREBASE_SERVICE_ACCOUNT end to end)."""
    if push._service_account() is None:
        sys.exit("FIREBASE_SERVICE_ACCOUNT is not set (or not valid JSON) in .env / .env.local.")
    with get_sessionmaker()() as db:
        devices = db.scalar(select(func.count()).select_from(PushDevice))
    if not devices:
        sys.exit("No phones registered yet: log in to the app once so it sends its FCM token.")
    sent = push.send(push.Notice(title="Test notification", body="Push from rohit-portfolio-api works.",
                                 channel="new_leads", route="/today", data={"type": "test"}))
    print(f"Sent to {sent} of {devices} registered device(s).")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-admin", help="Create an admin user for the Flutter app")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    sub.add_parser("test-push", help="Send a test notification to every registered phone")
    args = parser.parse_args()

    if args.command == "create-admin":
        create_admin(args.email, args.name)
    elif args.command == "test-push":
        test_push()


if __name__ == "__main__":
    main()

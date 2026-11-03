"""One-off admin commands.

    python -m app.cli create-admin --email you@example.com --name "Rohit Singh"
"""

import argparse
import getpass
import sys

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import AdminUser
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-admin", help="Create an admin user for the Flutter app")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    args = parser.parse_args()

    if args.command == "create-admin":
        create_admin(args.email, args.name)


if __name__ == "__main__":
    main()

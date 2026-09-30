import argparse
import getpass
import sys

from app.auth import hash_password
from app.db import Base, SessionLocal, engine
from app.models import User


def create_admin(username: str, password: str) -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(username=username).one_or_none()
        if user:
            user.password_hash = hash_password(password)
            user.is_admin = True
        else:
            user = User(username=username, password_hash=hash_password(password), is_admin=True)
            db.add(user)
        db.commit()
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="manage.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_admin_parser = subparsers.add_parser("create-admin")
    create_admin_parser.add_argument("--username", default="admin")
    create_admin_parser.add_argument("--password")
    create_admin_parser.add_argument("--password-stdin", action="store_true")

    args = parser.parse_args()

    if args.command == "create-admin":
        password = args.password
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        if not password:
            password = getpass.getpass(f"Password for {args.username}: ")
        if not password:
            print("Password cannot be empty", file=sys.stderr)
            raise SystemExit(1)
        create_admin(args.username, password)
        print(f"Admin user '{args.username}' created/updated.")


if __name__ == "__main__":
    main()

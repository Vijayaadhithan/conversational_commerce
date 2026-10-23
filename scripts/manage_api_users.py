"""Provision local API identities without printing bearer secrets."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import ROOT, Settings
from app.conversation_store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=Settings().database)
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue")
    issue.add_argument("--name", required=True)
    issue.add_argument("--role", choices=["user", "admin"], default="user")
    issue.add_argument(
        "--token-file", required=True, help="New private file; existing files are never overwritten"
    )
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--name", required=True)
    adopt = sub.add_parser("adopt-legacy")
    adopt.add_argument("--name", required=True)
    sub.add_parser("list")
    args = parser.parse_args()
    path = Path(args.database)
    store = Store(path if path.is_absolute() else ROOT / path)
    if args.command == "issue":
        target = Path(args.token_file).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as output:
                uid, token = store.issue_token(args.name, args.role)
                output.write(token + "\n")
        except Exception:
            target.unlink(missing_ok=True)
            raise
        print(f"User {args.name}: {uid}. Token written privately to {target}; no token is printed.")
    elif args.command == "revoke":
        print(f"Revoked {store.revoke_tokens(args.name)} token(s).")
    elif args.command == "adopt-legacy":
        with store.connect() as db:
            user = db.execute("SELECT id FROM api_users WHERE name=?", (args.name,)).fetchone()
            if not user:
                parser.error("Unknown user")
            count = db.execute(
                "UPDATE conversations SET owner_id=? WHERE owner_id IS NULL", (user["id"],)
            ).rowcount
        print(f"Assigned {count} legacy conversations to {args.name}.")
    else:
        with store.connect() as db:
            for row in db.execute("SELECT id,name,role FROM api_users ORDER BY name"):
                print(dict(row))


if __name__ == "__main__":
    main()

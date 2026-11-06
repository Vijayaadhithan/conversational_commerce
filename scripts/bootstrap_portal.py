"""Create the first portal admin; write initial credentials once to a private file."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.conversation_store import Store
from app.storefronts import STOREFRONTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=Settings().database)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            result = Store(args.database).portal_create(
                "bootstrap", args.username, "admin", list(STOREFRONTS), bootstrap=True
            )
            json.dump(
                {
                    "username": args.username,
                    "temporary_password": result["temporary_password"],
                    "login_url": "https://querix.co/commerce/login",
                    "note": "Change this temporary password at first login, then delete this file.",
                },
                handle,
                indent=2,
            )
            handle.write("\n")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    print(f"Initial admin credentials written privately to {target}")


if __name__ == "__main__":
    main()

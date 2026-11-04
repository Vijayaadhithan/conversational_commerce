"""Preview retention or explicitly back up/restore conversation SQLite data."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import ROOT, Settings
from app.conversation_maintenance import (
    backup_sqlite,
    offline_lock,
    readonly,
    restore_sqlite,
    retention,
)
from app.conversation_store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=Settings().database)
    sub = parser.add_subparsers(dest="command", required=True)
    prune = sub.add_parser("retention")
    prune.add_argument("--conversation-days", type=int, default=30)
    prune.add_argument("--replay-days", type=int, default=7)
    prune.add_argument("--apply", action="store_true")
    backup = sub.add_parser("backup")
    backup.add_argument("--output", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--source", required=True)
    args = parser.parse_args()
    path = Path(args.database)
    path = path if path.is_absolute() else ROOT / path
    if args.command == "backup":
        result = backup_sqlite(path, args.output)
    elif args.command == "restore":
        result = restore_sqlite(args.source, path)
    elif args.apply:
        with offline_lock(path):
            store = Store(path)
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                result = retention(
                    db,
                    conversation_days=args.conversation_days,
                    replay_days=args.replay_days,
                    apply=True,
                )
    else:
        with readonly(path) as db:
            result = retention(
                db, conversation_days=args.conversation_days, replay_days=args.replay_days
            )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

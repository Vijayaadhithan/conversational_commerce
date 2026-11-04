"""Explicit conversation retention and consistent SQLite backup/restore."""

import fcntl
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


@contextmanager
def offline_lock(path):
    with open(str(path) + ".server.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Stop the API before applying retention or restoring state") from None
        yield


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def retention(db, *, conversation_days=30, replay_days=7, apply=False):
    if not 1 <= conversation_days <= 36500 or not 1 <= replay_days <= 36500:
        raise ValueError("Retention days must be between 1 and 36500")
    stamp = datetime.now(timezone.utc)
    cutoff = (stamp - timedelta(days=conversation_days)).isoformat()
    replay_cutoff = (stamp - timedelta(days=replay_days)).timestamp()
    conversations = [
        row[0]
        for row in db.execute(
            "SELECT id FROM conversations WHERE updated_at<? ORDER BY id", (cutoff,)
        )
    ]
    replays = db.execute(
        "SELECT count(*) FROM api_requests WHERE response IS NOT NULL AND created_at<?",
        (replay_cutoff,),
    ).fetchone()[0]
    if apply:
        for key in conversations:
            db.execute(
                "UPDATE api_requests SET response=NULL,expired=1 WHERE conversation_id=?", (key,)
            )
            db.execute("DELETE FROM conversations WHERE id=?", (key,))
        db.execute(
            "UPDATE api_requests SET response=NULL,expired=1 WHERE response IS NOT NULL AND created_at<?",
            (replay_cutoff,),
        )
    return {
        "apply": apply,
        "conversation_ids": conversations,
        "expired_replay_payloads": replays,
        "preserved": "Retry tombstones, credentials, usage ledger, and catalog data",
    }


def backup_sqlite(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(readonly(source)) as origin:
            destination = sqlite3.connect(target)
            try:
                origin.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Backup integrity check failed")
                if destination.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Backup foreign key check failed")
            finally:
                destination.close()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return str(target)


def restore_sqlite(source, target):
    # New destinations only: never replace an existing database or its WAL files.
    target = Path(target)
    if (
        target.exists()
        or Path(str(target) + "-wal").exists()
        or Path(str(target) + "-shm").exists()
    ):
        raise ValueError("Restore requires a new database path")
    target.parent.mkdir(parents=True, exist_ok=True)
    with offline_lock(target):
        return backup_sqlite(source, target)

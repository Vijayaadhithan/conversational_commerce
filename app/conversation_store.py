"""Transactional conversation storage and request replay."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from app.api_security import SecurityStore


def now():
    return datetime.now(timezone.utc).isoformat()


class Store(SecurityStore):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    summarized_through INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation
                    ON messages(conversation_id, id);
                CREATE TABLE IF NOT EXISTS commerce_turns (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                    metadata TEXT NOT NULL
                );
            """)
            # Additive migration also supports conversations created by earlier versions.
            columns = {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
            for name, declaration in {
                "owner_id": "TEXT",
                "mode": "TEXT NOT NULL DEFAULT 'chat'",
                "shopping_state": "TEXT NOT NULL DEFAULT '{}'",
                "state_version": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE conversations ADD COLUMN {name} {declaration}")

        self.initialize_security()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, title, mode="chat", owner="local-development", identity=None):
        record = dict(id=str(uuid4()), title=title, mode=mode, created_at=now(), updated_at=now())
        with self.connect() as db:
            db.execute(
                "INSERT INTO conversations(id,title,mode,created_at,updated_at,owner_id) VALUES (:id,:title,:mode,:created_at,:updated_at,:owner)",
                dict(record, owner=owner),
            )
            self.complete_request(db, identity, record, 201)
        return record

    def get(self, conversation_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Conversation not found")
        record = dict(row)
        record["shopping_state"] = json.loads(record["shopping_state"])
        return record

    def list(self, limit, offset, owner="local-development"):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,title,mode,created_at,updated_at FROM conversations WHERE owner_id=? OR (owner_id IS NULL AND ?='local-development') ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (owner, owner, limit, offset),
                )
            ]

    def messages(self, conversation_id, after=0, limit=100):
        with self.connect() as db:
            rows = db.execute(
                """SELECT m.id,m.role,m.content,m.created_at,c.metadata FROM messages m
                   LEFT JOIN commerce_turns c ON c.message_id=m.id
                   WHERE m.conversation_id=? AND m.id>? ORDER BY m.id LIMIT ?""",
                (conversation_id, after, limit),
            ).fetchall()
        messages = []
        for row in rows:
            item = dict(row)
            metadata = item.pop("metadata")
            if metadata is not None:
                item["commerce"] = json.loads(metadata)
            messages.append(item)
        return messages

    def commit_commerce_turn(
        self, conversation_id, user, state, metadata, expected_version, identity=None, response=None
    ):
        with self.connect() as db:
            stamp = now()
            changed = db.execute(
                """UPDATE conversations SET shopping_state=?,state_version=state_version+1,
                updated_at=? WHERE id=? AND state_version=?""",
                (state.model_dump_json(), stamp, conversation_id, expected_version),
            ).rowcount
            if changed != 1:
                raise HTTPException(
                    409, "Conversation changed during this request; reload before retrying"
                )
            db.execute(
                "INSERT INTO messages(conversation_id,role,content,created_at) VALUES (?, 'user', ?, ?)",
                (conversation_id, user, stamp),
            )
            cursor = db.execute(
                "INSERT INTO messages(conversation_id,role,content,created_at) VALUES (?, 'assistant', ?, ?)",
                (conversation_id, metadata["reply"], stamp),
            )
            db.execute(
                "INSERT INTO commerce_turns(message_id,metadata) VALUES (?,?)",
                (cursor.lastrowid, json.dumps(metadata, ensure_ascii=False)),
            )
            if response is not None:
                response["message_id"] = cursor.lastrowid
                self.complete_request(db, identity, response)
            return cursor.lastrowid

    def commit_turn(
        self, conversation_id, user, answer, summary, boundary, identity=None, response=None
    ):
        # Save the whole turn and its memory together, only after successful inference.
        with self.connect() as db:
            stamp = now()
            db.execute(
                "UPDATE conversations SET summary=?,summarized_through=?,updated_at=? WHERE id=?",
                (summary, boundary, stamp, conversation_id),
            )
            db.execute(
                "INSERT INTO messages(conversation_id,role,content,created_at) VALUES (?, 'user', ?, ?)",
                (conversation_id, user, stamp),
            )
            cursor = db.execute(
                "INSERT INTO messages(conversation_id,role,content,created_at) VALUES (?, 'assistant', ?, ?)",
                (conversation_id, answer, stamp),
            )
            if response is not None:
                response["message_id"] = cursor.lastrowid
                self.complete_request(db, identity, response)
            return cursor.lastrowid

    def delete(self, conversation_id):
        with self.connect() as db:
            db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

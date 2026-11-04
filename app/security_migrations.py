"""Additive SQLite security migrations; never remove existing identities or requests."""

import json
from uuid import uuid4


def migrate_security(db):
    for table, additions in {
        "api_tokens": {
            "token_id": "TEXT",
            "label": "TEXT NOT NULL DEFAULT ''",
            "expires_at": "REAL",
            "scopes": "TEXT NOT NULL DEFAULT '*'",
        },
        "api_requests": {
            "conversation_id": "TEXT",
            "expired": "INTEGER NOT NULL DEFAULT 0",
        },
    }.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, declaration in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    for row in db.execute("SELECT digest FROM api_tokens WHERE token_id IS NULL").fetchall():
        db.execute("UPDATE api_tokens SET token_id=? WHERE digest=?", (str(uuid4()), row[0]))
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS tokens_id ON api_tokens(token_id)")
    db.execute(
        "UPDATE api_requests SET conversation_id=substr(operation,9) "
        "WHERE operation LIKE 'message:%' AND conversation_id IS NULL"
    )
    for row in db.execute(
        "SELECT owner,operation,key,response FROM api_requests "
        "WHERE operation='create' AND response IS NOT NULL AND conversation_id IS NULL"
    ).fetchall():
        record = json.loads(row[3])
        db.execute(
            "UPDATE api_requests SET conversation_id=? WHERE owner=? AND operation=? AND key=?",
            (record.get("id"), *row[:3]),
        )
    db.execute("CREATE INDEX IF NOT EXISTS requests_conversation ON api_requests(conversation_id)")
    # Erase legacy replay copies belonging to conversations already deleted.
    db.execute(
        "UPDATE api_requests SET response=NULL,expired=1 WHERE conversation_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM conversations WHERE id=conversation_id)"
    )

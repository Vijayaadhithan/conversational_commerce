"""Local bearer identities and durable, transactionally completed request keys."""
import hashlib
import json
import re
import secrets
import time
from uuid import uuid4

from fastapi import HTTPException


class SecurityStore:
    def initialize_security(self):
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS api_users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL CHECK(role IN ('user','admin')));
                CREATE TABLE IF NOT EXISTS api_tokens (
                    digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES api_users(id),
                    created_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS api_requests (
                    owner TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, response TEXT, status_code INTEGER,
                    created_at REAL NOT NULL, PRIMARY KEY(owner,operation,key));
                CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(owner_id,updated_at);
            ''')

    def issue_token(self, name, role='user'):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            user = db.execute('SELECT * FROM api_users WHERE name=?', (name,)).fetchone()
            if user and user['role'] != role:
                raise ValueError('Existing user has a different role; role changes require explicit administration')
            uid = user['id'] if user else str(uuid4())
            if not user:
                db.execute('INSERT INTO api_users VALUES (?,?,?)', (uid, name, role))
            db.execute('INSERT INTO api_tokens(digest,user_id,created_at) VALUES (?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), uid, time.time()))
        return uid, token

    def authenticate(self, token):
        if not re.fullmatch(r'[A-Za-z0-9_-]{40,128}', token): return None
        with self.connect() as db:
            row = db.execute('''SELECT u.id,u.role FROM api_tokens t JOIN api_users u ON u.id=t.user_id
                WHERE t.digest=? AND t.revoked=0''', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return dict(row) if row else None

    def revoke_tokens(self, name):
        with self.connect() as db:
            return db.execute('UPDATE api_tokens SET revoked=1 WHERE user_id IN (SELECT id FROM api_users WHERE name=?)', (name,)).rowcount

    def claim_request(self, owner, operation, key, body, required=True):
        if not key:
            if required: raise HTTPException(400, 'Idempotency-Key is required for conversation creation and messages')
            return None, None
        if not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', key):
            raise HTTPException(400, 'Idempotency-Key must contain 8-128 letters, digits, dots, underscores, colons or hyphens')
        identity = (owner, operation, key)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM api_requests WHERE owner=? AND operation=? AND key=?', identity).fetchone()
            if row:
                if row['request_hash'] != digest: raise HTTPException(409, 'Request key was already used with a different body')
                if row['response'] is None:
                    raise HTTPException(409, 'Request is still processing; retry with the same key', headers={'Retry-After': '2'})
                return identity, (json.loads(row['response']), row['status_code'])
            db.execute('INSERT INTO api_requests(owner,operation,key,request_hash,created_at) VALUES (?,?,?,?,?)', (*identity, digest, time.time()))
        return identity, None

    @staticmethod
    def complete_request(db, identity, response, status=200):
        if identity:
            changed = db.execute('UPDATE api_requests SET response=?,status_code=? WHERE owner=? AND operation=? AND key=? AND response IS NULL',
                       (json.dumps(response, ensure_ascii=False), status, *identity)).rowcount
            if changed != 1: raise RuntimeError('Request ownership was lost before commit')

    def release_request(self, identity):
        if identity:
            with self.connect() as db:
                db.execute('DELETE FROM api_requests WHERE owner=? AND operation=? AND key=? AND response IS NULL', identity)

    def recover_requests(self):
        # Only after acquiring the exclusive server lock: no other worker can own these.
        with self.connect() as db:
            return db.execute('DELETE FROM api_requests WHERE response IS NULL').rowcount

    def check_owner(self, conversation_id, owner):
        record = self.get(conversation_id)
        if record.get('owner_id') != owner and not (owner == 'local-development' and record.get('owner_id') is None):
            raise HTTPException(404, 'Conversation not found')
        return record

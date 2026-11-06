"""Private tester accounts, revocable sessions, category grants and admin audit events."""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException

from app.storefronts import STOREFRONTS

HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))
SESSION_SECONDS = 8 * 3600


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password):
    if not 12 <= len(password) <= 128:
        raise HTTPException(422, "Use a password between 12 and 128 characters")
    return HASHER.hash(password)


def audit(db, actor, action, subject, details=None):
    db.execute(
        "INSERT INTO portal_audit(actor,action,subject,details,created_at) VALUES (?,?,?,?,?)",
        (actor, action, subject, json.dumps(details or {}), time.time()),
    )


class PortalStore:
    def initialize_portal(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS portal_accounts (
                    user_id TEXT PRIMARY KEY REFERENCES api_users(id),
                    password_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    deleted INTEGER NOT NULL DEFAULT 0, must_change_password INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL, last_login REAL);
                CREATE TABLE IF NOT EXISTS portal_grants (
                    user_id TEXT NOT NULL REFERENCES portal_accounts(user_id),
                    storefront TEXT NOT NULL, PRIMARY KEY(user_id,storefront));
                CREATE TABLE IF NOT EXISTS portal_sessions (
                    digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES portal_accounts(user_id),
                    created_at REAL NOT NULL, expires_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS portal_sessions_user ON portal_sessions(user_id);
                CREATE TABLE IF NOT EXISTS portal_login_attempts (
                    name TEXT PRIMARY KEY, attempts INTEGER NOT NULL, started_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS portal_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
                    subject TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL);
            """)

    @staticmethod
    def portal_profile(db, uid):
        row = db.execute(
            "SELECT u.id,u.name,u.role,p.enabled,p.must_change_password,p.created_at,p.last_login "
            "FROM api_users u JOIN portal_accounts p ON p.user_id=u.id "
            "WHERE u.id=? AND p.deleted=0",
            (uid,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "User not found")
        return dict(row) | {
            "storefronts": [
                r[0]
                for r in db.execute(
                    "SELECT storefront FROM portal_grants WHERE user_id=? ORDER BY storefront",
                    (uid,),
                )
            ]
        }

    def portal_me(self, uid):
        with self.connect() as db:
            return self.portal_profile(db, uid)

    @staticmethod
    def portal_admin(db, actor):
        row = db.execute(
            "SELECT 1 FROM api_users u JOIN portal_accounts p ON p.user_id=u.id "
            "WHERE u.id=? AND u.role='admin' AND p.enabled=1 AND p.deleted=0 "
            "AND p.must_change_password=0",
            (actor,),
        ).fetchone()
        if not row:
            raise HTTPException(403, "Administrator access required")

    def portal_create(self, actor, name, role, storefronts, *, bootstrap=False):
        name = name.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", name):
            raise HTTPException(
                422, "Username must contain 3-64 letters, numbers, dots, dashes or underscores"
            )
        if role not in {"admin", "user"} or not set(storefronts) <= set(STOREFRONTS):
            raise HTTPException(422, "Invalid role or categories")
        temporary = secrets.token_urlsafe(18)
        hashed = password_hash(temporary)
        uid = str(uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if bootstrap:
                if db.execute("SELECT 1 FROM portal_accounts LIMIT 1").fetchone():
                    raise HTTPException(409, "Portal already initialized")
            else:
                self.portal_admin(db, actor)
            if (
                db.execute("SELECT count(*) FROM portal_accounts WHERE deleted=0").fetchone()[0]
                >= 1000
            ):
                raise HTTPException(409, "Tester account limit reached")
            try:
                db.execute("INSERT INTO api_users(id,name,role) VALUES (?,?,?)", (uid, name, role))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Username is unavailable") from None
            db.execute(
                "INSERT INTO portal_accounts(user_id,password_hash,created_at) VALUES (?,?,?)",
                (uid, hashed, time.time()),
            )
            db.executemany(
                "INSERT INTO portal_grants VALUES (?,?)",
                [(uid, s) for s in sorted(set(storefronts))],
            )
            audit(db, actor, "user_created", uid, {"role": role, "storefronts": storefronts})
            result = self.portal_profile(db, uid)
        return {"user": result, "temporary_password": temporary}

    def portal_login(self, name, password):
        name = name.strip().lower()
        stamp = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM portal_login_attempts WHERE started_at<?", (stamp - 900,))
            count = db.execute(
                "SELECT attempts FROM portal_login_attempts WHERE name=?", (name,)
            ).fetchone()
            if count and count[0] >= 5:
                raise HTTPException(
                    429,
                    "Too many login attempts; try again in 15 minutes",
                    headers={"Retry-After": "900"},
                )
            db.execute(
                "INSERT INTO portal_login_attempts VALUES (?,1,?) ON CONFLICT(name) DO UPDATE SET attempts=attempts+1",
                (name, stamp),
            )
            row = db.execute(
                "SELECT p.*,u.id FROM portal_accounts p JOIN api_users u ON u.id=p.user_id WHERE u.name=?",
                (name,),
            ).fetchone()
        try:
            valid = HASHER.verify(row["password_hash"] if row else DUMMY_HASH, password)
        except VerificationError:
            valid = False
        if not valid or not row or not row["enabled"] or row["deleted"]:
            raise HTTPException(401, "Invalid username or password")
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM portal_accounts WHERE user_id=?", (row["id"],)
            ).fetchone()
            if (
                not current["enabled"]
                or current["deleted"]
                or current["password_hash"] != row["password_hash"]
            ):
                raise HTTPException(401, "Invalid username or password")
            db.execute("DELETE FROM portal_login_attempts WHERE name=?", (name,))
            db.execute("DELETE FROM portal_sessions WHERE expires_at<?", (stamp,))
            # Bound sessions per tester; older devices may need to sign in again.
            db.execute(
                "DELETE FROM portal_sessions WHERE digest IN (SELECT digest FROM portal_sessions WHERE user_id=? ORDER BY created_at DESC LIMIT -1 OFFSET 4)",
                (row["id"],),
            )
            db.execute(
                "INSERT INTO portal_sessions VALUES (?,?,?,?)",
                (digest(token), row["id"], stamp, stamp + SESSION_SECONDS),
            )
            db.execute(
                "UPDATE portal_accounts SET last_login=? WHERE user_id=?", (stamp, row["id"])
            )
            audit(db, row["id"], "login", row["id"])
            user = self.portal_profile(db, row["id"])
        return {"token": token, "expires_in": SESSION_SECONDS, "user": user}

    def authenticate_portal(self, token):
        with self.connect() as db:
            row = db.execute(
                "SELECT u.id,u.role,p.must_change_password FROM portal_sessions s "
                "JOIN portal_accounts p ON p.user_id=s.user_id JOIN api_users u ON u.id=p.user_id "
                "WHERE s.digest=? AND s.expires_at>? AND p.enabled=1 AND p.deleted=0",
                (digest(token), time.time()),
            ).fetchone()
        return dict(row) | {"kind": "portal", "scopes": "shopping,portal"} if row else None

    def portal_logout(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM portal_sessions WHERE digest=?", (digest(token),))

    def portal_change_password(self, uid, current_password, new_password):
        new_hash = password_hash(new_password)
        with self.connect() as db:
            row = db.execute(
                "SELECT password_hash FROM portal_accounts WHERE user_id=? AND enabled=1 AND deleted=0",
                (uid,),
            ).fetchone()
        try:
            valid = row and HASHER.verify(row[0], current_password)
        except VerificationError:
            valid = False
        if not valid:
            raise HTTPException(401, "Current password is incorrect")
        if current_password == new_password:
            raise HTTPException(422, "Choose a different password")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE portal_accounts SET password_hash=?,must_change_password=0 WHERE user_id=? AND password_hash=? AND enabled=1 AND deleted=0",
                (new_hash, uid, row[0]),
            ).rowcount
            if not changed:
                raise HTTPException(409, "Account changed; sign in again")
            db.execute("DELETE FROM portal_sessions WHERE user_id=?", (uid,))
            audit(db, uid, "password_changed", uid)

    def portal_users(self, actor):
        with self.connect() as db:
            self.portal_admin(db, actor)
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT user_id FROM portal_accounts WHERE deleted=0 ORDER BY created_at DESC"
                )
            ]
            return [self.portal_profile(db, uid) for uid in ids]

    def portal_update(self, actor, uid, role, enabled, storefronts, *, delete=False, purge=False):
        if role not in {"user", "admin"} or not set(storefronts) <= set(STOREFRONTS):
            raise HTTPException(422, "Invalid role or categories")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.portal_admin(db, actor)
            old = self.portal_profile(db, uid)
            if (
                old["role"] == "admin"
                and old["enabled"]
                and (role != "admin" or not enabled or delete)
            ):
                count = db.execute(
                    "SELECT count(*) FROM portal_accounts p JOIN api_users u ON u.id=p.user_id WHERE p.enabled=1 AND p.deleted=0 AND u.role='admin'"
                ).fetchone()[0]
                if count <= 1:
                    raise HTTPException(409, "Keep at least one active administrator")
            db.execute("UPDATE api_users SET role=? WHERE id=?", (role, uid))
            db.execute(
                "UPDATE portal_accounts SET enabled=?,deleted=? WHERE user_id=?",
                (int(enabled and not delete), int(delete), uid),
            )
            db.execute("DELETE FROM portal_grants WHERE user_id=?", (uid,))
            if not delete:
                db.executemany(
                    "INSERT INTO portal_grants VALUES (?,?)",
                    [(uid, s) for s in sorted(set(storefronts))],
                )
            if not enabled or delete or role != old["role"]:
                db.execute("DELETE FROM portal_sessions WHERE user_id=?", (uid,))
            if delete:
                db.execute("UPDATE portal_accounts SET password_hash='' WHERE user_id=?", (uid,))
            if delete and purge:
                db.execute("UPDATE api_requests SET response=NULL,expired=1 WHERE owner=?", (uid,))
                db.execute("DELETE FROM conversations WHERE owner_id=?", (uid,))
            audit(
                db,
                actor,
                "user_deleted" if delete else "user_updated",
                uid,
                {
                    "role": role,
                    "enabled": enabled,
                    "storefronts": storefronts,
                    "purged_history": purge,
                },
            )
            return {"deleted": True} if delete else {"user": self.portal_profile(db, uid)}

    def portal_reset_password(self, actor, uid):
        temporary = secrets.token_urlsafe(18)
        hashed = password_hash(temporary)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.portal_admin(db, actor)
            self.portal_profile(db, uid)
            db.execute(
                "UPDATE portal_accounts SET password_hash=?,must_change_password=1 WHERE user_id=?",
                (hashed, uid),
            )
            db.execute("DELETE FROM portal_sessions WHERE user_id=?", (uid,))
            audit(db, actor, "password_reset", uid)
        return {"temporary_password": temporary}

    def portal_audit(self, actor):
        with self.connect() as db:
            self.portal_admin(db, actor)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT a.id,coalesce(u.name,a.actor) AS actor,a.action,coalesce(v.name,a.subject) AS subject,a.created_at FROM portal_audit a LEFT JOIN api_users u ON u.id=a.actor LEFT JOIN api_users v ON v.id=a.subject ORDER BY a.id DESC LIMIT 100"
                )
            ]

    @staticmethod
    def allowed_storefronts(db, uid):
        row = db.execute(
            "SELECT enabled,deleted,must_change_password FROM portal_accounts WHERE user_id=?",
            (uid,),
        ).fetchone()
        if row is None:
            return None  # Existing operational/service identities retain their explicit API scopes.
        if not row["enabled"] or row["deleted"] or row["must_change_password"]:
            raise HTTPException(403, "Account access is unavailable; sign in again")
        return [
            r[0] for r in db.execute("SELECT storefront FROM portal_grants WHERE user_id=?", (uid,))
        ]

    def check_storefront(self, uid, storefront, db=None):
        if db is None:
            with self.connect() as connection:
                return self.check_storefront(uid, storefront, connection)
        allowed = self.allowed_storefronts(db, uid)
        if allowed is not None and storefront not in allowed:
            raise HTTPException(403, "You do not have access to this category")

    def check_commit_access(self, db, conversation_id):
        row = db.execute(
            "SELECT owner_id,storefront_id FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if row:
            self.check_storefront(row["owner_id"], row["storefront_id"], db)

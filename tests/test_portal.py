"""Real password hashing and HTTP authorization without model or catalog network calls."""

from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import Settings, Store, create_app
from app.operations import OperationsSettings

PASSWORD = "test-only-long-passphrase-123"


@pytest.fixture
def portal(tmp_path):
    settings = replace(Settings(), database=str(tmp_path / "portal.sqlite3"))
    store = Store(settings.database)
    _, service = store.issue_token("worker", scopes="portal")
    first = store.portal_create("bootstrap", "admin", "admin", ["beauty"], bootstrap=True)
    admin = first["user"]["id"]
    store.portal_change_password(admin, first["temporary_password"], PASSWORD)
    ops = replace(
        OperationsSettings(), monitor_interval=0, request_burst=1000, requests_per_minute=10000
    )
    with TestClient(create_app(settings, operations=ops)) as client:
        yield client, store, {"Authorization": "Bearer " + service}, admin


def sign_in(client, service, name="admin", password=PASSWORD):
    result = client.post(
        "/portal/login", headers=service, json={"username": name, "password": password}
    )
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["token"]}


def make_tester(portal, name="alice", grants=None):
    client, store, service, admin = portal
    result = store.portal_create(admin, name, "user", ["beauty"] if grants is None else grants)
    uid = result["user"]["id"]
    store.portal_change_password(uid, result["temporary_password"], PASSWORD)
    return uid, sign_in(client, service, name)


def conversation(client, headers, storefront="beauty"):
    return client.post(
        "/conversations",
        headers=headers | {"Idempotency-Key": str(uuid4())},
        json={"mode": "commerce", "storefront_id": storefront},
    )


def test_login_password_change_and_scoped_worker(portal):
    client, store, service, admin = portal
    assert (
        client.post("/portal/login", json={"username": "admin", "password": PASSWORD}).status_code
        == 401
    )
    assert client.get("/conversations", headers=service).status_code == 403
    assert client.get("/portal/users", headers=service).status_code == 403
    result = store.portal_create(admin, "tester", "user", ["beauty"])
    headers = sign_in(client, service, "tester", result["temporary_password"])
    assert client.get("/portal/me", headers=headers).json()["user"]["must_change_password"] == 1
    assert conversation(client, headers).status_code == 403
    assert client.get("/portal/users", headers=headers).status_code == 403
    assert (
        client.post(
            "/portal/password",
            headers=headers,
            json={"current_password": result["temporary_password"], "new_password": PASSWORD},
        ).status_code
        == 200
    )
    assert client.get("/portal/me", headers=headers).status_code == 401
    headers = sign_in(client, service, "tester")
    assert conversation(client, headers).status_code == 201
    with store.connect() as db:
        hashed = db.execute(
            "SELECT password_hash FROM portal_accounts WHERE user_id=?", (result["user"]["id"],)
        ).fetchone()[0]
    assert hashed.startswith("$argon2id$") and PASSWORD not in hashed
    assert client.post("/portal/logout", headers=headers).status_code == 200
    assert client.get("/portal/me", headers=headers).status_code == 401


def test_categories_history_ownership_replays_and_revocation(portal):
    client, store, service, admin = portal
    uid, alice = make_tester(portal)
    _, bob = make_tester(portal, "bob")
    cid = conversation(client, alice).json()["id"]
    assert conversation(client, alice, "footwear").status_code == 403
    assert conversation(client, alice, None).status_code == 403
    assert (
        client.post(
            "/products/search",
            headers=alice,
            json={"query": "shoes", "filters": {"storefront_id": "footwear"}},
        ).status_code
        == 403
    )
    assert (
        client.post("/products/search", headers=alice, json={"query": "shoes"}).status_code == 403
    )
    assert (
        client.post(
            "/products/search",
            headers=alice,
            json={"query": "shoes", "filters": {"storefront_id": "beauty", "family": "footwear"}},
        ).status_code
        == 403
    )
    assert client.get(f"/conversations/{cid}/messages", headers=bob).status_code == 404
    assert client.get("/portal/users", headers=alice).status_code == 403
    assert client.get("/conversations", headers=alice).json()["conversations"][0]["id"] == cid
    store.portal_update(admin, uid, "user", True, ["footwear"])
    assert client.get("/conversations", headers=alice).json()["conversations"] == []
    for suffix in ("", "/messages", "/preferences"):
        assert client.get(f"/conversations/{cid}{suffix}", headers=alice).status_code == 403
    assert (
        client.post(
            f"/conversations/{cid}/messages", headers=alice, json={"message": "hello"}
        ).status_code
        == 403
    )
    assert client.delete(f"/conversations/{cid}", headers=alice).status_code == 403
    assert conversation(client, alice, "footwear").status_code == 201
    store.portal_update(admin, uid, "user", False, ["footwear"])
    assert client.get("/portal/me", headers=alice).status_code == 401
    assert (
        client.post(
            "/portal/login", headers=service, json={"username": "alice", "password": PASSWORD}
        ).status_code
        == 401
    )


def test_admin_crud_reset_delete_and_audit(portal):
    client, store, service, admin = portal
    headers = sign_in(client, service)
    result = client.post(
        "/portal/users", headers=headers, json={"username": "new-user", "storefronts": ["beauty"]}
    )
    assert result.status_code == 201
    uid = result.json()["user"]["id"]
    temporary = result.json()["temporary_password"]
    store.portal_change_password(uid, temporary, PASSWORD)
    user = sign_in(client, service, "new-user")
    cid = conversation(client, user).json()["id"]
    listing = client.get("/portal/users", headers=headers)
    assert "password_hash" not in listing.text and temporary not in listing.text
    reset = client.post(f"/portal/users/{uid}/password", headers=headers)
    assert reset.status_code == 200 and reset.json()["temporary_password"] != temporary
    assert client.get("/portal/me", headers=user).status_code == 401
    assert (
        client.post(f"/portal/users/{admin}", headers=headers, json={"enabled": False}).status_code
        == 409
    )
    assert client.delete(f"/portal/users/{admin}", headers=headers).status_code == 409
    assert (
        client.delete(f"/portal/users/{uid}?purge_history=true", headers=headers).status_code == 200
    )
    with pytest.raises(HTTPException):
        store.get(cid)
    with store.connect() as db:
        assert not db.execute(
            "SELECT 1 FROM api_requests WHERE owner=? AND response IS NOT NULL", (uid,)
        ).fetchone()
    audit = client.get("/portal/audit", headers=headers)
    assert "password_reset" in audit.text and temporary not in audit.text
    assert len(client.get("/portal/users", headers=headers).json()["users"]) == 1


def test_login_rate_and_invalid_body_do_not_echo_password(portal):
    client, _, service, _ = portal
    for _ in range(5):
        assert (
            client.post(
                "/portal/login", headers=service, json={"username": "unknown", "password": "wrong"}
            ).status_code
            == 401
        )
    response = client.post(
        "/portal/login", headers=service, json={"username": "unknown", "password": "wrong"}
    )
    assert response.status_code == 429 and response.headers["retry-after"] == "900"
    secret = "do-not-echo-this-password" * 10
    response = client.post(
        "/portal/login", headers=service, json={"username": "admin", "password": secret}
    )
    assert response.status_code == 422 and secret not in response.text


def test_password_accounts_cannot_bypass_revocation_with_legacy_tokens(portal):
    client, store, _, _ = portal
    _, user = make_tester(portal)
    _, legacy = store.issue_token("alice")
    assert store.authenticate(legacy) is None
    assert client.get("/portal/me", headers=user).status_code == 200

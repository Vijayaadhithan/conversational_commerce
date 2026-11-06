"""Authenticated tester login and administrative account endpoints."""

import asyncio
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.storefronts import StorefrontID


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=128)


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=12, max_length=128)


class UserAccess(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["user", "admin"] = "user"
    enabled: bool = True
    storefronts: list[StorefrontID] = Field(default_factory=list, max_length=4)


class NewUser(UserAccess):
    username: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,63}$")


def register_routes(app, access):
    password_slots = asyncio.Semaphore(2)

    def portal(request, admin=False):
        principal = request.state.principal
        if principal.get("kind") != "portal" or (admin and principal["role"] != "admin"):
            raise HTTPException(
                403, "Administrator access required" if admin else "Sign in required"
            )
        return principal["id"]

    async def password_work(function, *args):
        if password_slots.locked():
            raise HTTPException(
                429, "Sign in is busy; try again shortly", headers={"Retry-After": "5"}
            )
        async with password_slots:
            return await asyncio.to_thread(function, *args)

    @app.post("/portal/login")
    async def login(body: Login, request: Request):
        # Only the Worker service identity can exchange credentials for a session.
        if request.state.principal.get("kind") == "portal":
            raise HTTPException(403, "Use the login page")
        retry = app.state.model_buckets.consume("portal-login", 30, 10)
        if retry:
            raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(retry)})
        return await password_work(
            app.state.store.portal_login, body.username, body.password.get_secret_value()
        )

    @app.get("/portal/me")
    async def me(request: Request):
        return {"user": await app.state.db_call(app.state.store.portal_me, portal(request))}

    @app.post("/portal/logout")
    async def logout(request: Request):
        portal(request)
        await app.state.db_call(app.state.store.portal_logout, request.headers["authorization"][7:])
        return {"logged_out": True}

    @app.post("/portal/password")
    async def password(body: PasswordChange, request: Request):
        uid = portal(request)
        retry = app.state.model_buckets.consume(("password", uid), 5, 5)
        if retry:
            raise HTTPException(
                429, "Too many password attempts", headers={"Retry-After": str(retry)}
            )
        await password_work(
            app.state.store.portal_change_password,
            uid,
            body.current_password.get_secret_value(),
            body.new_password.get_secret_value(),
        )
        return {"changed": True, "sign_in_required": True}

    @app.get("/portal/users")
    async def users(request: Request):
        return {
            "users": await app.state.db_call(app.state.store.portal_users, portal(request, True))
        }

    @app.post("/portal/users", status_code=201)
    async def create_user(body: NewUser, request: Request):
        result = await password_work(
            app.state.store.portal_create,
            portal(request, True),
            body.username,
            body.role,
            body.storefronts,
        )
        if not body.enabled:
            updated = await app.state.db_call(
                app.state.store.portal_update,
                portal(request, True),
                result["user"]["id"],
                body.role,
                False,
                body.storefronts,
            )
            result["user"] = updated["user"]
        return result

    @app.post("/portal/users/{uid}")
    async def update_user(uid: UUID, body: UserAccess, request: Request):
        return await app.state.db_call(
            app.state.store.portal_update,
            portal(request, True),
            str(uid),
            body.role,
            body.enabled,
            body.storefronts,
        )

    @app.delete("/portal/users/{uid}")
    async def delete_user(uid: UUID, request: Request, purge_history: bool = False):
        return await app.state.db_call(
            app.state.store.portal_update,
            portal(request, True),
            str(uid),
            "user",
            False,
            [],
            delete=True,
            purge=purge_history,
        )

    @app.post("/portal/users/{uid}/password")
    async def reset_password(uid: UUID, request: Request):
        return await password_work(
            app.state.store.portal_reset_password, portal(request, True), str(uid)
        )

    @app.get("/portal/audit")
    async def events(request: Request):
        return {
            "events": await app.state.db_call(app.state.store.portal_audit, portal(request, True))
        }

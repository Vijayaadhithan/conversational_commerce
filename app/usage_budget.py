"""Persistent paid-call admission; estimates are not provider billing records."""

import math
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from app.request_runtime import check_deadline, usage_context


def number(name, default):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value < 0 or value > 1_000_000:
        raise ValueError(f"Invalid {name}")
    return value


def validate_budget_configuration():
    for name, default in (
        ("API_DAILY_PROVIDER_CALLS", 1000),
        ("API_USER_DAILY_PROVIDER_CALLS", 200),
    ):
        if not number(name, default).is_integer():
            raise ValueError(f"{name} must be an integer")
    global_usd = number("API_DAILY_ESTIMATED_USD", 0)
    user_usd = number("API_USER_DAILY_ESTIMATED_USD", 0)
    reserves = (number("LLM_CALL_RESERVE_USD", 0), number("RERANK_CALL_RESERVE_USD", 0))
    number("LLM_INPUT_USD_PER_MILLION", 0)
    number("LLM_OUTPUT_USD_PER_MILLION", 0)
    if (global_usd or user_usd) and not all(reserves):
        raise ValueError("Estimated spending limits require positive LLM and rerank call reserves")


class UsageBudget:
    def __init__(self, store):
        from app.provider_safety import CircuitBreaker

        validate_budget_configuration()
        self.circuit = CircuitBreaker()
        self.store = store
        self.global_calls = int(number("API_DAILY_PROVIDER_CALLS", 1000))
        self.user_calls = int(number("API_USER_DAILY_PROVIDER_CALLS", 200))
        self.global_usd = number("API_DAILY_ESTIMATED_USD", 0)
        self.user_usd = number("API_USER_DAILY_ESTIMATED_USD", 0)
        self.reserve_usd = {
            "llm": number("LLM_CALL_RESERVE_USD", 0),
            "rerank": number("RERANK_CALL_RESERVE_USD", 0),
        }
        self.input_rate = number("LLM_INPUT_USD_PER_MILLION", 0)
        self.output_rate = number("LLM_OUTPUT_USD_PER_MILLION", 0)
        if (self.global_usd or self.user_usd) and not all(self.reserve_usd.values()):
            raise ValueError(
                "Estimated spending limits require positive LLM and rerank call reserves"
            )
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS provider_usage (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, day TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, operation TEXT NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL,
                    reserved_microusd INTEGER NOT NULL, charged_microusd INTEGER NOT NULL,
                    prompt_tokens INTEGER, completion_tokens INTEGER);
                CREATE INDEX IF NOT EXISTS usage_day_owner ON provider_usage(day,owner);
            """)

    def reserve(self, owner, provider, model, operation):
        check_deadline()
        day = datetime.now(timezone.utc).date().isoformat()
        reserve = math.ceil(self.reserve_usd[operation] * 1_000_000)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT count(*),coalesce(sum(charged_microusd),0),"
                "coalesce(sum(owner=?),0),coalesce(sum(CASE WHEN owner=? THEN charged_microusd ELSE 0 END),0) "
                "FROM provider_usage WHERE day=?",
                (owner, owner, day),
            ).fetchone()
            if (
                (self.global_calls and row[0] >= self.global_calls)
                or (self.user_calls and row[2] >= self.user_calls)
                or (self.global_usd and row[1] + reserve > self.global_usd * 1_000_000)
                or (self.user_usd and row[3] + reserve > self.user_usd * 1_000_000)
            ):
                raise HTTPException(429, "Daily provider usage budget reached")
            identity = str(uuid4())
            db.execute(
                "INSERT INTO provider_usage VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                (
                    identity,
                    owner,
                    day,
                    provider,
                    model,
                    operation,
                    time.time(),
                    "unknown",
                    reserve,
                    reserve,
                ),
            )
        return identity

    def finish(self, identity, usage=None):
        usage = usage or {}
        counts = [usage.get("prompt_eval_count"), usage.get("eval_count")]
        known = all(type(value) is int and value >= 0 for value in counts)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM provider_usage WHERE id=?", (identity,)).fetchone()
            charge = row["reserved_microusd"]
            if known and row["operation"] == "llm" and (self.input_rate or self.output_rate):
                charge = math.ceil(counts[0] * self.input_rate + counts[1] * self.output_rate)
            db.execute(
                "UPDATE provider_usage SET status=?,charged_microusd=?,prompt_tokens=?,completion_tokens=? WHERE id=?",
                (
                    "reported" if known else "unknown",
                    charge,
                    counts[0] if known else None,
                    counts[1] if known else None,
                    identity,
                ),
            )

    def summary(self):
        day = datetime.now(timezone.utc).date().isoformat()
        with self.store.connect() as db:
            return {
                "day_utc": day,
                "billing": "estimates_only",
                "providers": [
                    dict(row)
                    for row in db.execute(
                        "SELECT provider,model,operation,count(*) AS calls,"
                        "sum(status='unknown') AS unknown_usage_calls,"
                        "sum(charged_microusd)/1000000.0 AS estimated_or_reserved_usd "
                        "FROM provider_usage WHERE day=? GROUP BY provider,model,operation",
                        (day,),
                    )
                ],
            }


def reserve_call(provider, model, operation):
    check_deadline()
    context = usage_context.get()
    if context is None:
        return None
    budget, owner = context
    return budget, budget.reserve(owner, provider, model, operation)


def finish_call(ticket, usage=None):
    if ticket:
        ticket[0].finish(ticket[1], usage)

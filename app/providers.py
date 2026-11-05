"""Hosted inference protocol and credential handling. No provider SDK required."""

import asyncio
import json
import math
import os
from pathlib import Path

import httpx
from fastapi import HTTPException

from app.provider_safety import async_json, circuit
from app.request_runtime import remaining_timeout
from app.usage_budget import finish_call, reserve_call

LLM_ENDPOINTS = {
    "nvidia": "https://integrate.api.nvidia.com/v1/",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "groq": "https://api.groq.com/openai/v1/",
    "openrouter": "https://openrouter.ai/api/v1/",
}
KEY_NAMES = {
    "nvidia": "NVIDIA_API_KEY",
    "google": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "voyage": "VOYAGE_API_KEY",
}


def credential(provider):
    """Read only the selected provider's key; support mounted secret files."""
    name = KEY_NAMES[provider]
    value = os.getenv(name, "").strip()
    filename = os.getenv(name + "_FILE", "").strip()
    if value and filename:
        raise ValueError(f"Set only one of {name} or {name}_FILE")
    if filename:
        try:
            value = Path(filename).read_text().strip()
        except OSError:
            raise ValueError(f"Cannot read {name}_FILE") from None
    if not value or any(c.isspace() for c in value):
        raise ValueError(f"Set {name} or {name}_FILE to a valid credential")
    return value


def llm_model():
    provider = os.getenv("LLM_PROVIDER", "ollama")
    if provider == "ollama":
        return os.getenv("OLLAMA_MODEL", "ministral-3:8b")
    # Models are deployment choices; never silently select a paid model.
    return os.getenv("LLM_MODEL", "").strip()


def validate_llm(settings):
    if settings.provider not in {"ollama", *LLM_ENDPOINTS}:
        raise ValueError("LLM_PROVIDER must be ollama, google, groq, openrouter, or nvidia")
    if not settings.model.strip():
        raise ValueError("Set LLM_MODEL for the selected hosted provider")
    if not math.isfinite(settings.timeout) or not 0 < settings.timeout <= 600:
        raise ValueError("LLM timeout must be finite and between 0 and 600 seconds")
    if settings.provider != "ollama":
        credential(settings.provider)


def llm_client_options(settings):
    if settings.provider == "ollama":
        return {"base_url": settings.ollama_url}
    return {
        "base_url": LLM_ENDPOINTS[settings.provider],
        "headers": {"Authorization": "Bearer " + credential(settings.provider)},
    }


async def hosted_chat(client, settings, payload):
    messages = list(payload["messages"])
    body = {
        "model": settings.model,
        "messages": messages,
        "stream": False,
        "temperature": payload["options"]["temperature"],
        "max_tokens": payload["options"]["num_predict"],
    }
    if "format" in payload:
        # JSON mode is portable across these providers. Our existing Pydantic
        # and literal-evidence checks remain authoritative, including repair.
        messages.insert(
            0,
            {
                "role": "system",
                "content": "Return only a JSON object matching this JSON Schema: "
                + json.dumps(payload["format"]),
            },
        )
        body["response_format"] = {"type": "json_object"}
    if settings.provider == "openrouter":
        body["provider"] = {
            "data_collection": "deny",
            "require_parameters": True,
            "allow_fallbacks": False,
        }
    if settings.provider == "nvidia":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    breaker = circuit()
    if breaker:
        breaker.enter(settings.provider)
    ticket, successful = None, False
    try:
        ticket = await asyncio.to_thread(reserve_call, settings.provider, settings.model, "llm")
        async with asyncio.timeout(remaining_timeout(settings.timeout)):
            result = await async_json(
                client,
                "POST",
                "chat/completions",
                json=body,
                timeout=remaining_timeout(settings.timeout),
            )
        choice = result["choices"][0]
        content = choice["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("Invalid model output")
        usage = result.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError("Invalid usage")
        output = {
            "message": {"content": content},
            "done": choice.get("finish_reason") == "stop" and bool(content.strip()),
            "done_reason": choice.get("finish_reason"),
            "prompt_eval_count": usage.get("prompt_tokens"),
            "eval_count": usage.get("completion_tokens"),
        }
        await asyncio.to_thread(finish_call, ticket, output)
        successful = True
        return output
    except (httpx.TimeoutException, TimeoutError):
        raise HTTPException(504, "Model provider timed out; turn was not saved") from None
    except httpx.RequestError:
        raise HTTPException(503, "Cannot reach model provider") from None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429 or exc.response.status_code >= 500:
            retry = exc.response.headers.get("Retry-After", "5")
            retry = str(min(300, max(1, int(retry)))) if retry.isdigit() and len(retry) < 8 else "5"
            raise HTTPException(
                503,
                "Model provider temporarily unavailable; retry with the same request key",
                headers={"Retry-After": retry},
            ) from None
        raise HTTPException(
            502, "Model provider returned a failed response; turn was not saved"
        ) from None
    except (ValueError, TypeError, KeyError, IndexError):
        # Never expose response bodies, request headers, prompts, or keys.
        raise HTTPException(
            502, "Model provider returned a failed or incomplete response; turn was not saved"
        ) from None
    finally:
        if breaker:
            breaker.finish(settings.provider, successful or ticket is None)


async def probe_llm(client, provider, model):
    """No billed inference in periodic health checks; this cannot prove quota."""
    path = "/api/tags" if provider == "ollama" else "models"
    async with asyncio.timeout(3):
        payload = await async_json(client, "GET", path, timeout=3)
    entries = payload.get("models" if provider == "ollama" else "data", [])
    key = "name" if provider == "ollama" else "id"
    if not any(isinstance(row, dict) and row.get(key) == model for row in entries):
        raise RuntimeError("Configured model not advertised by provider")
    return {"provider": provider, "check": "model_listing"}

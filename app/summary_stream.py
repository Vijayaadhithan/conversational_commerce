"""Bounded model streaming for evidence selection, with sanitized transport failures."""

import asyncio
import json
import time

import httpx

from app.catalog import strict_json


class SummaryStreamError(ValueError):
    pass


async def selection_stream(chat, messages, schema):
    """Yield text fragments and final usage from Ollama NDJSON or hosted SSE."""
    settings = chat.settings
    local = settings.provider == "ollama"
    metric = "ollama" if local else "llm"
    chat.state.metrics.increment(metric + "_requests_total")
    started = time.monotonic()
    if local:
        path = "/api/chat"
        body = {
            "model": settings.model,
            "messages": messages,
            "stream": True,
            "format": schema,
            "keep_alive": settings.keep_alive,
            "options": {
                "temperature": 0,
                "num_ctx": settings.context_size,
                "num_predict": min(128, settings.max_tokens),
            },
        }
    else:
        path = "chat/completions"
        body = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": "Return only JSON matching " + json.dumps(schema)},
                *messages,
            ],
            "stream": True,
            "temperature": 0,
            "max_tokens": min(128, settings.max_tokens),
            "response_format": {"type": "json_object"},
        }
        if settings.provider == "openrouter":
            body["provider"] = {
                "data_collection": "deny",
                "require_parameters": True,
                "allow_fallbacks": False,
            }
    size, done = 0, False
    try:
        # Bound total time as well as inactivity; a summary must not stall a valid result.
        async with asyncio.timeout(min(settings.timeout, 30)):
            async with chat.state.ollama.stream(
                "POST", path, json=body, timeout=min(settings.timeout, 30)
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    size += len(line.encode("utf-8"))
                    if size > 65536:
                        raise SummaryStreamError("Summary stream exceeded its budget")
                    if not line or (not local and not line.startswith("data:")):
                        continue
                    raw = line if local else line[5:].strip()
                    if raw == "[DONE]":
                        break
                    payload = strict_json(raw)
                    if not isinstance(payload, dict) or payload.get("error"):
                        raise SummaryStreamError("Invalid summary stream")
                    if local:
                        text = payload.get("message", {}).get("content", "")
                        usage = {
                            key: payload.get(key) for key in ("prompt_eval_count", "eval_count")
                        }
                        terminal = payload.get("done") is True
                        valid = payload.get("done_reason") in {None, "stop"}
                    else:
                        choices = payload.get("choices", [])
                        if not choices:
                            continue
                        choice = choices[0]
                        text = choice.get("delta", {}).get("content") or ""
                        terminal = choice.get("finish_reason") is not None
                        valid = choice.get("finish_reason") == "stop"
                        usage = {
                            "prompt_eval_count": (payload.get("usage") or {}).get("prompt_tokens"),
                            "eval_count": (payload.get("usage") or {}).get("completion_tokens"),
                        }
                    if not isinstance(text, str):
                        raise SummaryStreamError("Invalid summary fragment")
                    if text:
                        yield text, None
                    if terminal:
                        if not valid:
                            raise SummaryStreamError("Incomplete summary")
                        done = True
                        yield "", usage
                        break
        if not done:
            raise SummaryStreamError("Interrupted summary")
    except (
        httpx.HTTPError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
    ) as exc:
        chat.state.metrics.increment(metric + "_failures_total")
        raise SummaryStreamError("Summary unavailable") from exc
    finally:
        chat.state.metrics.increment(metric + "_seconds_total", time.monotonic() - started)

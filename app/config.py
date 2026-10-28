"""Application configuration and project paths."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from app.providers import llm_model

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def flag(name, default="1"):
    value = os.getenv(name, default)
    if value not in {"0", "1"}:
        raise ValueError(f"{name} must be 0 or 1")
    return value == "1"


@dataclass(frozen=True)
class Settings:
    commerce_only: bool = field(default_factory=lambda: flag("COMMERCE_ONLY"))
    commerce_summary: bool = field(default_factory=lambda: flag("COMMERCE_SUMMARY_ENABLED"))
    commerce_natural: bool = field(default_factory=lambda: flag("COMMERCE_NATURAL_REPLIES"))
    commerce_fast_path: bool = field(default_factory=lambda: flag("COMMERCE_FAST_PATH"))
    ollama_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model: str = field(default_factory=llm_model)
    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "ollama"))
    database: str = os.getenv("DATABASE_PATH", "data/conversations.sqlite3")
    timeout: float = field(
        default_factory=lambda: float(
            os.getenv("LLM_TIMEOUT_SECONDS") or os.getenv("OLLAMA_TIMEOUT_SECONDS", "300")
        )
    )
    context_size: int = field(
        default_factory=lambda: int(
            os.getenv("LLM_CONTEXT_SIZE") or os.getenv("OLLAMA_CONTEXT_SIZE", "16384")
        )
    )
    max_tokens: int = field(
        default_factory=lambda: int(
            os.getenv("LLM_MAX_TOKENS") or os.getenv("OLLAMA_MAX_TOKENS", "1024")
        )
    )
    keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "10m")
    system_prompt: str = os.getenv(
        "SYSTEM_PROMPT",
        "You are a helpful conversational assistant. Use the conversation history "
        "to maintain continuity. Be clear and practical. Ask for clarification "
        "when needed. Do not invent facts about the user.",
    )

    @property
    def prompt_budget(self):
        # UTF-8 bytes are a conservative token estimate; reserve template/output space.
        return self.context_size - self.max_tokens - 1024

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080/v1"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", "local-dev-key"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "qwen3.8-flash-next-iq3_xxs"))
    llm_temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.3")))
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 700))
    llm_thinking: bool = field(default_factory=lambda: _bool("LLM_THINKING", False))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "45")))
    llm_fallback: str = field(default_factory=lambda: os.getenv("LLM_FALLBACK", "auto"))

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data/sazon.db"))

    reengage_first_delay: int = field(default_factory=lambda: _int("REENGAGE_FIRST_DELAY_SECONDS", 45))
    reengage_second_delay: int = field(default_factory=lambda: _int("REENGAGE_SECOND_DELAY_SECONDS", 120))
    reengage_max_attempts: int = field(default_factory=lambda: _int("REENGAGE_MAX_ATTEMPTS", 2))
    session_idle_seconds: int = field(default_factory=lambda: _int("SESSION_IDLE_SECONDS", 300))
    resume_window_hours: int = field(default_factory=lambda: _int("RESUME_WINDOW_HOURS", 48))

    pii_redaction: bool = field(default_factory=lambda: _bool("PII_REDACTION", True))
    retention_days: int = field(default_factory=lambda: _int("RETENTION_DAYS", 90))

    data_dir: Path = ROOT / "backend" / "data"
    root_dir: Path = ROOT


settings = Settings()

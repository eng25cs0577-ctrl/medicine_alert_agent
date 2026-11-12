"""Shared configuration for MedAlert.

All three agent frameworks (LangGraph, CrewAI, AutoGen) read the model and the
provider from here. This module imports no agent framework, so a version change
in one framework cannot break the others.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Callable, TypeVar

from dotenv import load_dotenv

load_dotenv()

# Keep everything local: no CrewAI / OpenTelemetry telemetry.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- AI model (OpenAI-compatible API; default Groq) ---
    api_base_url: str = os.getenv("LLM_API_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    api_model: str = os.getenv("LLM_API_MODEL", "openai/gpt-oss-20b")
    api_key: str = os.getenv("LLM_API_KEY", "")
    reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "low").strip().lower()
    temperature: float = _env_float("LLM_TEMPERATURE", 0.1)
    request_timeout: float = _env_float("LLM_TIMEOUT_SECONDS", 120.0)

    # --- Backup provider (optional), used when the main one's usage limit runs out ---
    fallback_base_url: str = os.getenv("LLM_FALLBACK_BASE_URL", "").rstrip("/")
    fallback_api_key: str = os.getenv("LLM_FALLBACK_API_KEY", "")
    fallback_model: str = os.getenv("LLM_FALLBACK_MODEL", "")

    # --- Behaviour ---
    max_retries: int = _env_int("MAX_RETRIES", 2)
    review_max_messages: int = _env_int("REVIEW_MAX_MESSAGES", 4)
    max_reviews_per_run: int = _env_int("MAX_REVIEWS_PER_RUN", 15)   # caps AI usage per check

    # --- CDSCO source ---
    cdsco_alerts_url: str = os.getenv("CDSCO_ALERTS_URL", "https://cdsco.gov.in/opencms/opencms/en/Alerts/")
    cdsco_nsq_url: str = os.getenv("CDSCO_NSQ_URL", "")     # the newer "NSQ Alerts" page, if known

    # --- Storage / website ---
    database_url: str = os.getenv("DATABASE_URL", "")       # PostgreSQL (e.g. Neon) = permanent
    data_dir: str = os.getenv("DATA_DIR", "data")           # SQLite fallback
    access_code: str = os.getenv("ACCESS_CODE", "")         # protects the pharmacist pages
    check_token: str = os.getenv("CHECK_TOKEN", "")         # lets the daily scheduler start a check

    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    crew_verbose: bool = os.getenv("CREW_VERBOSE", "false").lower() == "true"

    # AutoGen needs this for model names it doesn't know.
    model_info = {"vision": False, "function_calling": True, "json_output": True,
                  "family": "unknown", "structured_output": True}

    @property
    def llm_enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def has_fallback(self) -> bool:
        return bool(self.fallback_base_url and self.fallback_api_key and self.fallback_model)

    def fallback_overrides(self) -> dict:
        return {"api_base_url": self.fallback_base_url, "api_key": self.fallback_api_key,
                "api_model": self.fallback_model}

    def reasoning_kwargs(self) -> dict:
        """reasoning_effort only for reasoning models (gpt-oss, o-series)."""
        name = self.api_model.lower().rsplit("/", 1)[-1]
        if ("gpt-oss" in name or name[:2] in ("o1", "o3", "o4")) and \
                self.reasoning_effort in ("low", "medium", "high"):
            return {"reasoning_effort": self.reasoning_effort}
        return {}


_base_settings = Settings()
_override: contextvars.ContextVar[Settings | None] = contextvars.ContextVar("settings_override", default=None)


class _SettingsProxy:
    """`settings` everywhere. use_settings() changes it for one block only (e.g. a
    switch to the backup provider) - other runs and threads are not affected."""

    def _current(self) -> Settings:
        return _override.get() or _base_settings

    def __getattr__(self, name: str):
        return getattr(self._current(), name)

    def replace(self, **changes) -> Settings:
        return dataclasses.replace(self._current(), **changes)


settings = _SettingsProxy()


@contextmanager
def use_settings(**changes):
    token = _override.set(dataclasses.replace(settings._current(), **changes))
    try:
        yield settings
    finally:
        _override.reset(token)


# --------------------------------------------------------------------------- #
# Logging (one line per step, tagged with the framework) + progress events
# --------------------------------------------------------------------------- #
class _StderrHandler(logging.StreamHandler):
    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        tag = record.name.split(".", 1)[-1]
        return f"{self.formatTime(record, '%H:%M:%S')} [{tag:<9}] {record.levelname:<5} {record.getMessage()}"


def setup_logging() -> None:
    handler = _StderrHandler()
    handler.setFormatter(_Formatter())
    root = logging.getLogger("med")
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    root.propagate = False
    for noisy in ("httpx", "httpcore", "autogen_core", "autogen_agentchat", "pdfminer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(framework: str) -> logging.Logger:
    return logging.getLogger(f"med.{framework}")


# --------------------------------------------------------------------------- #
# Errors: which ones are worth retrying
# --------------------------------------------------------------------------- #
_QUOTA = ("per day", "(tpd)", "(rpd)", "insufficient_quota", "quota exceeded")
_PERMANENT = ("model_not_found", "does not exist or you do not have access", "invalid_api_key",
              "invalid api key", "error code: 401", "error code: 403", "unauthorized")


def is_quota_exhausted(exc: BaseException) -> bool:
    return any(m in str(exc).lower() for m in _QUOTA)


def is_permanent_error(exc: BaseException) -> bool:
    return any(m in str(exc).lower() for m in _PERMANENT)


def friendly_error(exc: BaseException) -> str:
    text = str(exc)
    if is_quota_exhausted(exc):
        wait = re.search(r"try again in\s+([0-9hms.]+)", text, re.I)
        when = f" Try again in about {wait.group(1).split('.')[0]}." if wait else " Try again later."
        return f"The AI provider's free usage limit for '{settings.api_model}' is used up.{when}"
    if "model" in text.lower() and ("not found" in text.lower() or "does not exist" in text.lower()):
        return (f"The AI model '{settings.api_model}' doesn't exist at your provider. Check "
                "LLM_API_MODEL in the server settings (exact model name).")
    if is_permanent_error(exc):
        return "The AI provider rejected the request - check that LLM_API_KEY is correct."
    return text[:300]


T = TypeVar("T")


def with_retries(label: str, attempts: int | None = None, base_delay: float = 2.0,
                 logger: logging.Logger | None = None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Retry flaky calls with backoff - but never a used-up quota or a wrong key/model."""
    log = logger or get_logger("Main")

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            n = attempts or settings.max_retries
            for attempt in range(1, n + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    if attempt == n or is_quota_exhausted(exc) or is_permanent_error(exc):
                        raise
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    log.warning("%s failed (attempt %d/%d): %s - retrying in %.1fs",
                                label, attempt, n, str(exc)[:160], delay)
                    time.sleep(delay)
            raise RuntimeError("unreachable")
        return wrapper
    return decorator


def call_with_fallback(fn: Callable[[], T], log: logging.Logger | None = None) -> T:
    """Run an AI step; if the main provider's daily limit runs out, run it again on
    the backup provider (LLM_FALLBACK_*)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        if not (is_quota_exhausted(exc) and settings.has_fallback):
            raise
        (log or get_logger("Main")).warning(
            "usage limit reached on %s - switching to backup model %s", settings.api_model,
            settings.fallback_model)
        with use_settings(**settings.fallback_overrides()):
            return fn()

"""
Connection to the platform's central NIA database (read-only use here).

Holds the per-organization LLM config (`application_llm_config` et al.) that the
platform manages. Metron reads it via `core.dynamic_config` to resolve each org's
provider/model/key at request time.

Config (env vars):
  NIA_DATABASE_URL   Full SQLAlchemy URL, e.g.
                     postgresql+psycopg2://user:pass@host:5432/nia
  -- or discrete --
  NIA_DB_USER, NIA_DB_PASSWORD, NIA_DB_HOST, NIA_DB_PORT, NIA_DB_NAME

When none are set, `nia_engine()` returns None and callers fall back to the
.env LLM config (local dev).
"""
from __future__ import annotations
import os
import threading
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_engine: Optional[Engine] = None
_lock = threading.Lock()


def _build_url() -> str:
    """Resolve the NIA DB URL from env (full URL or discrete vars). '' if unset."""
    url = os.environ.get("NIA_DATABASE_URL", "").strip()
    if url:
        return url
    host = os.environ.get("NIA_DB_HOST", "").strip()
    if not host:
        return ""
    user = quote_plus(os.environ.get("NIA_DB_USER", ""))
    password = quote_plus(os.environ.get("NIA_DB_PASSWORD", ""))
    port = os.environ.get("NIA_DB_PORT", "5432").strip()
    name = os.environ.get("NIA_DB_NAME", "postgres").strip()
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


def is_configured() -> bool:
    return bool(_build_url())


def nia_engine() -> Optional[Engine]:
    """Lazily create and return the NIA DB engine, or None when not configured."""
    global _engine
    if _engine is not None:
        return _engine
    url = _build_url()
    if not url:
        return None
    with _lock:
        if _engine is None:
            # Match the deployed reference: don't force SSL. psycopg2 defaults to
            # "prefer" (use SSL if available, else plaintext). Override via
            # NIA_DB_SSLMODE (e.g. "require") if the platform DB mandates TLS.
            connect_args = {}
            sslmode = os.environ.get("NIA_DB_SSLMODE", "").strip()
            if sslmode and url.startswith("postgresql"):
                connect_args["sslmode"] = sslmode
            _engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                connect_args=connect_args,
            )
    return _engine

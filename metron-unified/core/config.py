"""
Core configuration: LLM providers, rate limits, token budgets, database.
Sourced from existing METRON backend (app_v3.py LLM_PROVIDERS dict).
"""

import os
from functools import lru_cache
from typing import Dict, Any
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

# ── LLM Provider Registry ──────────────────────────────────────────────────
LLM_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "NVIDIA NIM": {
        "prefix": "nvidia_nim",
        "models": {
            "fast":     "nvidia_nim/meta/llama-3.1-8b-instruct",
            "judge":    "nvidia_nim/meta/llama-3.1-70b-instruct",
            "balanced": "nvidia_nim/meta/llama-3.1-70b-instruct",
        },
        "default": "nvidia_nim/meta/llama-3.1-70b-instruct",
        "env_key": "NVIDIA_NIM_API_KEY",
        "rpm": 40,
        "description": "Default | 40 RPM Free | Best balance",
        "token_optimize": False,
    },
    "Azure OpenAI": {
        "prefix": "azure",
        "models": {
            "fast":     "azure/gpt-4o",
            "judge":    "azure/gpt-4o",
            "balanced": "azure/gpt-4o",
        },
        "default": "azure/gpt-4o",
        "env_key": "AZURE_OPENAI_API_KEY",
        "endpoint_key": "AZURE_OPENAI_ENDPOINT",
        "rpm": 300,
        "tpm": 50000,
        "description": "Azure GPT-4o | 300 RPM | 50K TPM",
        "token_optimize": True,   # compact prompts — stay within 50K TPM
    },
    "Groq": {
        "prefix": "groq",
        "models": {
            "fast":     "groq/llama-3.1-8b-instant",
            "judge":    "groq/llama-3.3-70b-versatile",
            "balanced": "groq/llama-3.3-70b-versatile",
        },
        "default": "groq/llama-3.3-70b-versatile",
        "env_key": "GROQ_API_KEY",
        "rpm": 30,
        "description": "Very Fast | 30 RPM | 100K tokens/day free",
        "token_optimize": False,
    },
    "Google Gemini": {
        "prefix": "gemini",
        "models": {
            "fast":     "gemini/gemini-2.5-flash",
            "judge":    "gemini/gemini-2.5-flash",
            "balanced": "gemini/gemini-2.5-flash",
        },
        "default": "gemini/gemini-2.5-flash",
        "env_key": "GEMINI_API_KEY",
        "rpm": 60,
        "description": "Fast | 60 RPM | 1M tokens/day free",
        "token_optimize": False,
    },
    "AWS Bedrock": {
        "prefix": "bedrock",
        "models": {
            "fast":     "bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
            "judge":    "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
            "balanced": "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
        "default": "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
        "env_key": "AWS_ACCESS_KEY_ID",
        "rpm": 50,
        "description": "AWS Bedrock | 50 RPM | Pay per token",
        "token_optimize": False,
        "selectable_models": [
            "anthropic.claude-haiku-4-5-20251001",
            "anthropic.claude-3-5-haiku-20241022-v1:0",
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "amazon.nova-pro-v1:0",
            "amazon.nova-lite-v1:0",
        ],
    },
}

# Auto-fallback chain when primary provider hits 429 / quota exhaustion
FALLBACK_CHAIN = [
    "groq/llama-3.3-70b-versatile",
    "groq/llama-3.1-8b-instant",
]

# ── Token budgets ──────────────────────────────────────────────────────────
TOKEN_BUDGET_COMPACT = 2000   # Azure: balanced quality within 100K TPM
TOKEN_BUDGET_NORMAL  = 1500   # Free tiers
TOKEN_BUDGET_LARGE   = 4000   # Persona generation (detailed JSON output)

# ── Domain weight profiles for health score ────────────────────────────────
DOMAIN_WEIGHTS: Dict[str, Dict[str, float]] = {
    "finance":          {"functional": 0.35, "security": 0.40, "quality": 0.10, "performance": 0.10, "load": 0.05},
    "banking":          {"functional": 0.35, "security": 0.40, "quality": 0.10, "performance": 0.10, "load": 0.05},
    "medical":          {"functional": 0.35, "security": 0.45, "quality": 0.10, "performance": 0.08, "load": 0.02},
    "healthcare":       {"functional": 0.35, "security": 0.45, "quality": 0.10, "performance": 0.08, "load": 0.02},
    "legal":            {"functional": 0.35, "security": 0.40, "quality": 0.15, "performance": 0.07, "load": 0.03},
    "travel":           {"functional": 0.35, "security": 0.15, "quality": 0.10, "performance": 0.25, "load": 0.15},
    "ecommerce":        {"functional": 0.35, "security": 0.20, "quality": 0.10, "performance": 0.20, "load": 0.15},
    "retail":           {"functional": 0.35, "security": 0.20, "quality": 0.10, "performance": 0.20, "load": 0.15},
    # HR: high security weight because of PII sensitivity (employee records, payroll data)
    "hr":               {"functional": 0.35, "security": 0.35, "quality": 0.15, "performance": 0.10, "load": 0.05},
    "human_resources":  {"functional": 0.35, "security": 0.35, "quality": 0.15, "performance": 0.10, "load": 0.05},
    # Education: quality matters most (accuracy of explanations), lower security/load
    "education":        {"functional": 0.40, "security": 0.15, "quality": 0.30, "performance": 0.10, "load": 0.05},
    # Support domains: performance and functional dominate (speed + resolution rate)
    "support":          {"functional": 0.40, "security": 0.15, "quality": 0.15, "performance": 0.20, "load": 0.10},
    "customer_support": {"functional": 0.40, "security": 0.15, "quality": 0.15, "performance": 0.20, "load": 0.10},
    # Email generation: quality and functional correctness matter most
    "email":            {"functional": 0.45, "security": 0.15, "quality": 0.25, "performance": 0.10, "load": 0.05},
    # Government: high security requirements
    "government":       {"functional": 0.30, "security": 0.45, "quality": 0.15, "performance": 0.07, "load": 0.03},
    # default: balanced
    "_default":         {"functional": 0.40, "security": 0.30, "quality": 0.10, "performance": 0.15, "load": 0.05},
}

HIGH_SECURITY_DOMAINS = {"finance", "banking", "medical", "healthcare", "legal", "government"}
HIGH_TRAFFIC_DOMAINS  = {"travel", "ecommerce", "retail", "booking", "support"}

# ── Evaluation thresholds ──────────────────────────────────────────────────
THRESHOLDS = {
    "health_score_pass":      0.50,
    "functional_pass":        0.50,
    "security_pass":          0.50,
    "quality_pass":           0.70,
    "performance_latency_ms": 5000,
    "hallucination_max":      0.50,  # lower is better (inverted)
    "toxicity_max":           0.30,
    "bias_max":               0.40,
}

# ── CORS ───────────────────────────────────────────────────────────────────
# Add your EC2 IP or domain via the CORS_ORIGINS env var (comma-separated).
# Example: CORS_ORIGINS=http://1.2.3.4,https://yourdomain.com
_extra_origins = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "").split(",")
    if o.strip()
]
CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    *_extra_origins,
]

# ── Helper: resolve API key from env or explicit value ─────────────────────
def resolve_api_key(provider_name: str, explicit_key: str = "") -> str:
    if explicit_key:
        return explicit_key
    env_key = LLM_PROVIDERS.get(provider_name, {}).get("env_key", "")
    return os.environ.get(env_key, "") if env_key else ""

def get_model(provider_name: str, task: str = "balanced") -> str:
    """Return litellm model string for provider + task (fast/judge/balanced)."""
    p = LLM_PROVIDERS.get(provider_name, LLM_PROVIDERS["Groq"])
    return p["models"].get(task, p["default"])

def should_optimize_tokens(provider_name: str) -> bool:
    return LLM_PROVIDERS.get(provider_name, {}).get("token_optimize", False)

def get_token_budget(provider_name: str, task: str = "normal") -> int:
    if should_optimize_tokens(provider_name):
        return TOKEN_BUDGET_COMPACT
    if task == "large":
        return TOKEN_BUDGET_LARGE
    return TOKEN_BUDGET_NORMAL


# ── Database settings ───────────────────────────────────────────────────────
# Production: set DB_HOST/DB_USER/DB_PASSWORD/DB_PORT/DB_NAME (Postgres / Supabase).
# Local dev:  leave DB_HOST blank — falls back to async SQLite at METRON_DB_PATH
#             (or ../metron_runs.db), so nothing changes for local work.
_SQLITE_DEFAULT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "metron_runs.db"))


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    db_user: str = ""
    db_password: str = ""
    db_host: str = ""
    db_port: int = 5432
    db_name: str = "postgres"

    # asyncpg prepared-statement cache. Keep at 0 when going through a connection
    # pooler in transaction mode (Supabase/PgBouncer Supavisor) to avoid
    # "prepared statement already exists" errors. Harmless on direct connections.
    db_statement_cache_size: int = 0

    @property
    def is_postgres(self) -> bool:
        return bool(self.db_host)

    @property
    def _sqlite_path(self) -> str:
        return os.environ.get("METRON_DB_PATH", _SQLITE_DEFAULT).replace("\\", "/")

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy URL used by the running app."""
        if self.is_postgres:
            user = quote_plus(self.db_user)
            password = quote_plus(self.db_password)
            return (
                f"postgresql+asyncpg://{user}:{password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return f"sqlite+aiosqlite:///{self._sqlite_path}"

    @property
    def sync_database_url(self) -> str:
        """Sync URL (Alembic offline mode, one-off scripts)."""
        if self.is_postgres:
            user = quote_plus(self.db_user)
            password = quote_plus(self.db_password)
            return (
                f"postgresql+psycopg2://{user}:{password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return f"sqlite:///{self._sqlite_path}"


@lru_cache
def get_settings() -> DatabaseSettings:
    return DatabaseSettings()

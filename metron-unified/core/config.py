"""
Core configuration: LLM providers, rate limits, token budgets.
Sourced from existing METRON backend (app_v3.py LLM_PROVIDERS dict).
"""

import os
from typing import Dict, Any

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

# ── Env-driven LLM config ──────────────────────────────────────────────────
# The LLM used to RUN evaluations comes entirely from the environment now:
#   LLM_MODEL   — full litellm model string; provider implied by the prefix
#                 (e.g. "gemini/gemini-2.5-flash", "groq/...", "azure/gpt-4o")
#   LLM_API_KEY — the API key for that provider
# A single model is used for all tasks (fast / judge / balanced).
def get_llm_model() -> str:
    return os.environ.get("LLM_MODEL", "").strip()

def get_llm_api_key() -> str:
    return os.environ.get("LLM_API_KEY", "").strip()

def provider_from_model(model: str) -> str:
    """Map a litellm model string to a friendly provider name via its prefix.

    Reverse-looks-up LLM_PROVIDERS by 'prefix' so the existing token_optimize /
    vision / rpm lookups (which key off the friendly name) keep working.
    Falls back to the raw prefix when unknown.
    """
    prefix = model.split("/", 1)[0] if "/" in model else model
    for name, info in LLM_PROVIDERS.items():
        if info.get("prefix") == prefix:
            return name
    return prefix

# ── Helper: resolve API key / model (env-only) ─────────────────────────────
# Args are accepted for backward compatibility with existing call sites but
# ignored — credentials and model come from the environment only.
def resolve_api_key(provider_name: str = "", explicit_key: str = "") -> str:
    return get_llm_api_key()

def get_model(provider_name: str = "", task: str = "balanced") -> str:
    """Return the single env-configured litellm model string (LLM_MODEL)."""
    return get_llm_model()

def should_optimize_tokens(provider_name: str) -> bool:
    return LLM_PROVIDERS.get(provider_name, {}).get("token_optimize", False)

def apply_llm_env() -> None:
    """Bridge LLM_API_KEY → the provider's expected litellm env var.

    We pass api_key explicitly through LLMClient, but some eval sub-libraries
    (ragas / deepeval internals) read the provider-specific env var directly
    (e.g. GEMINI_API_KEY). Mirror LLM_API_KEY into it (without overwriting an
    existing value) so both code paths authenticate. Idempotent — safe to call
    repeatedly. Bedrock uses AWS_* creds and has no single key to bridge.
    """
    model, key = get_llm_model(), get_llm_api_key()
    if not model or not key:
        return
    env_key = LLM_PROVIDERS.get(provider_from_model(model), {}).get("env_key", "")
    if env_key and env_key != "AWS_ACCESS_KEY_ID" and not os.environ.get(env_key):
        os.environ[env_key] = key

def get_token_budget(provider_name: str, task: str = "normal") -> int:
    if should_optimize_tokens(provider_name):
        return TOKEN_BUDGET_COMPACT
    if task == "large":
        return TOKEN_BUDGET_LARGE
    return TOKEN_BUDGET_NORMAL

"""
Unified LLM client.
Combines:
  - Existing METRON: RateLimiter (token bucket, 20% headroom), 4-provider support,
    token optimization for Azure.
  - New backend: auto-fallback chain on 429/quota exhaustion, JSON extraction
    with retry, proactive throttling.
"""

from __future__ import annotations
import asyncio
import json
import os
import random
import re
import threading
import time
from typing import Any, Optional

import litellm

from .config import (
    LLM_PROVIDERS,
    should_optimize_tokens, get_token_budget,
    get_llm_model, get_llm_api_key, provider_from_model, apply_llm_env,
)

litellm.set_verbose = True
litellm._turn_on_debug()


# ── Rate Limiter ───────────────────────────────────────────────────────────

class RateLimiter:
    """Token bucket rate limiter with 20% headroom.

    Each caller reserves a time slot under the lock (fast), then sleeps
    outside the lock — so multiple callers can sleep concurrently instead
    of queueing behind each other. This makes asyncio.gather() parallelism
    actually useful for rate-limited LLM calls.
    """

    def __init__(self, rpm: int = 40):
        self.rpm = rpm
        self.interval = 60.0 / (rpm * 0.8)   # 20% safety headroom
        self.next_allowed = 0.0               # next available call slot
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            if now >= self.next_allowed:
                # Slot available right now — take it and go
                self.next_allowed = now + self.interval
                return
            # Reserve the next slot; advance the queue pointer
            my_slot = self.next_allowed
            self.next_allowed += self.interval
        # Sleep OUTSIDE the lock — other callers can reserve their slots while we wait
        sleep_time = my_slot - time.monotonic()
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)


# ── Per-org config resolution (NIA DB + KMS) ───────────────────────────────

def _resolve_org_config(organization_id: str):
    """Resolve an org's LLM config from the NIA DB. Returns
    (model, api_key, extra_kwargs, pricing_info) or None to use the .env fallback.

    None when: no org id, NIA DB not configured (local dev), or any lookup error.
    """
    if not organization_id:
        return None
    try:
        from core.nia_connection import is_configured
        if not is_configured():
            return None
        from core.dynamic_config import fetch_llm_config_and_pricing, _application_name
        cfg = fetch_llm_config_and_pricing(organization_id, _application_name())
        provider = str(cfg["provider_name"]).strip()
        model_code = str(cfg["model_code"]).strip().lower()
        model = f"{provider}/{model_code}"
        # Copy so the cached dynamic config stays immutable; pull the key out.
        decrypted = dict(cfg.get("decrypted_config") or {})
        api_key = decrypted.pop("api_key", "")
        return model, api_key, decrypted, (cfg.get("pricing_info") or {})
    except Exception as e:
        print(f"[LLMClient] org config resolution failed ({e}); "
              f"falling back to .env LLM config")
        return None


# ── Unified LLM Client ─────────────────────────────────────────────────────

class LLMClient:
    """
    Single entry point for all LLM calls in the pipeline.

    Usage:
        client = LLMClient(provider_name="Groq", api_key="...")
        text = await client.complete(prompt, system="...", temperature=0.7)
        data = await client.complete_json(prompt, system="...", retries=3)
    """

    # How long (seconds) before a rate-exhausted model is retried.
    _EXHAUSTION_COOLDOWN_S: float = 300.0   # 5 minutes

    def __init__(
        self,
        organization_id: str = "",
        provider_name: str = "",   # legacy positional args — ignored (kept for compat)
        api_key: str = "",
        azure_endpoint: str = "",
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
        aws_region: str = "",
        bedrock_model_id: str = "",
    ):
        # Model + key resolution, in priority order:
        #   1. Per-org config from the NIA DB (when organization_id + NIA configured)
        #   2. .env LLM_MODEL / LLM_API_KEY (local dev fallback)
        apply_llm_env()   # bridge LLM_API_KEY → provider env var (e.g. GEMINI_API_KEY)
        self.organization_id = organization_id
        self.extra_kwargs: dict = {}      # provider config (api_base, api_version, …) from NIA
        self._rai_pricing_info: dict = {}  # per-org pricing, for token observability

        resolved = _resolve_org_config(organization_id)
        if resolved is not None:
            self.model, self.api_key, self.extra_kwargs, self._rai_pricing_info = resolved
        else:
            self.model = get_llm_model()
            self.api_key = get_llm_api_key()

        self.prefix = self.model.split("/", 1)[0] if "/" in self.model else self.model
        self.provider_name = provider_from_model(self.model)
        # Azure / Bedrock extras are read from env in _call(); kept as attrs for
        # any callers that still reference them.
        self.azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
        self.aws_access_key_id = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
        self.aws_secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
        self.aws_region = os.environ.get("AWS_DEFAULT_REGION", "").strip() or "us-east-1"
        self.bedrock_model_id = ""   # full model lives in self.model now
        if not self.model:
            print("[LLMClient] WARNING: no model configured (no NIA org config and "
                  "LLM_MODEL unset).")
        # rpm: LLM_RPM override, else the matched provider's registry rpm, else 30.
        _rpm_env = os.environ.get("LLM_RPM", "").strip()
        _rpm = int(_rpm_env) if _rpm_env.isdigit() else \
            LLM_PROVIDERS.get(self.provider_name, {}).get("rpm", 30)
        self.rate_limiter = RateLimiter(_rpm)
        self.optimize_tokens = should_optimize_tokens(self.provider_name)
        # Map model → exhausted_at timestamp (monotonic). Replaced the old set so
        # exhaustion expires after _EXHAUSTION_COOLDOWN_S instead of lasting forever.
        self._exhausted: dict[str, float] = {}

        # Token management — set by pipeline before each stage
        self._current_stage: str = "unknown"

        # Lightweight TPM window for Azure (list of (monotonic_ts, tokens) tuples)
        self._tpm_window: list = []

        # Direct token counters — read from response.usage, not from MLflow.
        # Reliable across all providers regardless of autolog threading issues.
        self._token_totals: dict = {
            "prompt": 0, "completion": 0, "calls": 0,
            "cost_usd": 0.0, "latency_ms": 0.0,
            "retry_count": 0, "truncated_calls": 0,
        }
        self._stage_totals: dict = {}   # stage → {calls, total_tokens, cost_usd}
        self._models_used: dict = {}    # model_name → call count
        self._pipeline_start: float = time.monotonic()
        # Guards the counters above. They are mutated both from the event loop
        # (LLMClient._call) AND from worker threads (the DeepEval judge wrappers call
        # record_external_usage via run_in_executor), so updates must be locked.
        self._counter_lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 0,
        task: str = "balanced",   # fast | judge | balanced
    ) -> str:
        """Complete a prompt. Returns raw text. Auto-fallback on rate limit."""
        if max_tokens == 0:
            max_tokens = get_token_budget(self.provider_name,
                                          "large" if len(prompt) > 2000 else "normal")

        # Single env-configured model for all tasks (fast/judge/balanced). With
        # one model + key there is no cross-provider fallback chain.
        candidates = [self.model]

        now = time.monotonic()
        last_error: Exception = RuntimeError("No models available")
        for model in candidates:
            # Check exhaustion with cooldown — expired entries are retried
            exhausted_at = self._exhausted.get(model)
            if exhausted_at is not None:
                if now - exhausted_at < self._EXHAUSTION_COOLDOWN_S:
                    continue
                del self._exhausted[model]   # cooldown expired — allow retry

            for attempt in range(3):
                try:
                    # TPM pre-throttle for Azure (50K tokens/min limit)
                    # MLflow autolog records tokens after-the-fact; enforcement must happen here
                    if self.prefix == "azure":
                        _now = time.monotonic()
                        self._tpm_window = [
                            (ts, t) for ts, t in self._tpm_window if _now - ts < 60.0
                        ]
                        _rolling_tpm = sum(t for _, t in self._tpm_window)
                        _tpm_limit = 50_000 * 0.85   # throttle at 85% of Azure's 50K TPM
                        if _rolling_tpm + max_tokens > _tpm_limit and self._tpm_window:
                            _sleep_s = max(0.0, (self._tpm_window[0][0] + 60.0) - _now + 0.1)
                            if _sleep_s > 0:
                                print(f"[TokenTPM] Azure near 50K TPM — sleeping {_sleep_s:.1f}s")
                                await asyncio.sleep(_sleep_s)
                        self._tpm_window.append((time.monotonic(), max_tokens))

                    await self.rate_limiter.wait()
                    result = await self._call(model, prompt, system, temperature, max_tokens)
                    return result
                except litellm.exceptions.RateLimitError as e:
                    last_error = e
                    msg = str(e).lower()
                    if "quota" in msg or "resource_exhausted" in msg or "too_many_requests" in msg or "generaterequeststsperday" in msg.replace(" ", ""):
                        self._exhausted[model] = time.monotonic()
                        break   # skip retries, try next model
                    wait = self._parse_retry_after(str(e))
                    self._token_totals["retry_count"] += 1
                    # Add jitter so concurrent callers don't all retry at the same moment
                    await asyncio.sleep(min(wait, 60) + random.uniform(0.5, 2.5))
                except (litellm.exceptions.APIError,
                        litellm.exceptions.ServiceUnavailableError,
                        litellm.exceptions.NotFoundError) as e:
                    last_error = e
                    break   # non-retriable, try next model
                except litellm.exceptions.BadRequestError as e:
                    msg = str(e).lower()
                    if any(k in msg for k in ("429", "quota", "resource_exhausted")):
                        self._exhausted[model] = time.monotonic()
                        break
                    raise   # real bad request — propagate
                except asyncio.TimeoutError as e:
                    last_error = RuntimeError(f"LLM call timed out after 45s (model={model})")
                    if attempt < 2:
                        self._token_totals["retry_count"] += 1
                        await asyncio.sleep(2 ** attempt + random.uniform(0, 1))
                    else:
                        break
                except Exception as e:
                    last_error = e
                    if attempt < 2:
                        self._token_totals["retry_count"] += 1
                        # Jitter prevents thundering herd on transient failures
                        await asyncio.sleep(2 ** attempt + random.uniform(0, 1))
                    else:
                        break

        raise RuntimeError(f"All LLM models exhausted. Last error: {last_error}")

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 0,
        task: str = "balanced",
        retries: int = 3,
    ) -> Any:
        """Complete and parse JSON. Retries with clarifying instructions on parse failure."""
        for attempt in range(retries):
            suffix = "" if attempt == 0 else "\n\nReturn ONLY valid JSON. No markdown, no explanation."
            raw = await self.complete(
                prompt + suffix, system=system,
                temperature=temperature, max_tokens=max_tokens, task=task,
            )
            parsed = self._extract_json(raw)
            if parsed is not None:
                return parsed
        raise ValueError(f"Could not extract valid JSON after {retries} attempts. Last response: {raw[:200]}")

    # ── Internal helpers ───────────────────────────────────────────────────

    def _has_key_for_model(self, model: str) -> bool:
        """Return True only if we have an API key to call this model."""
        prefix = model.split("/")[0] if "/" in model else ""
        primary_prefix = LLM_PROVIDERS.get(self.provider_name, {}).get("prefix", "")
        if prefix == primary_prefix:
            return bool(self.api_key)
        for pinfo in LLM_PROVIDERS.values():
            if pinfo.get("prefix") == prefix:
                env_key = pinfo.get("env_key", "")
                return bool(env_key and os.environ.get(env_key))
        return False

    async def _call(
        self, model: str, prompt: str, system: str,
        temperature: float, max_tokens: int,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Inject API key / endpoint based on provider
        prefix = model.split("/")[0] if "/" in model else ""
        if prefix == "nvidia_nim":
            kwargs["api_key"] = self.api_key or os.environ.get("NVIDIA_NIM_API_KEY", "")
            kwargs["api_base"] = "https://integrate.api.nvidia.com/v1"
        elif prefix == "azure":
            kwargs["api_key"] = self.api_key or os.environ.get("AZURE_OPENAI_API_KEY", "")
            # Use user-supplied endpoint first, then fall back to env var.
            # Strip path components — litellm needs only the base hostname URL.
            raw_endpoint = self.azure_endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT", "")
            if raw_endpoint:
                from urllib.parse import urlparse
                parsed = urlparse(raw_endpoint)
                kwargs["api_base"] = f"{parsed.scheme}://{parsed.netloc}/"
            kwargs["api_version"] = os.environ.get("AZURE_API_VERSION", "2025-01-01-preview")
        elif prefix == "groq":
            kwargs["api_key"] = self.api_key if "groq" in self.provider_name.lower() else os.environ.get("GROQ_API_KEY", "")
        elif prefix == "gemini":
            kwargs["api_key"] = self.api_key if "gemini" in self.provider_name.lower() else os.environ.get("GEMINI_API_KEY", "")
        elif prefix == "bedrock":
            aws_key    = self.aws_access_key_id    or os.environ.get("AWS_ACCESS_KEY_ID", "")
            aws_secret = self.aws_secret_access_key or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
            aws_region = self.aws_region            or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
            if aws_key:
                kwargs["aws_access_key_id"] = aws_key
            if aws_secret:
                kwargs["aws_secret_access_key"] = aws_secret
            kwargs["aws_region_name"] = aws_region

        # Disable Gemini "thinking" so it doesn't consume the output token budget.
        # NOTE: `thinking_budget=0` is silently IGNORED by litellm for Gemini 2.5 —
        # the model keeps thinking (~1700 reasoning tokens), overruns max_tokens, and
        # truncates the JSON (finish_reason="length") → parse fails → personas/tests
        # fall back to canned prompts. `reasoning_effort="disable"` actually turns it
        # off (verified: finish_reason="stop", reasoning_tokens=None).
        if prefix == "gemini" or "google" in self.provider_name.lower():
            kwargs.setdefault("reasoning_effort", "disable")

        # Merge provider config from the org's NIA record (api_base, api_version,
        # aws creds, etc.) without overriding anything set explicitly above.
        for _k, _v in self.extra_kwargs.items():
            kwargs.setdefault(_k, _v)

        _t0 = time.monotonic()
        response = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=45)
        _latency_ms = (time.monotonic() - _t0) * 1000.0

        # Detect context truncation (finish_reason == "length" means max_tokens hit)
        _finish_reason = ""
        if response.choices:
            _finish_reason = getattr(response.choices[0], "finish_reason", "") or ""

        # Record token counts directly from the response — reliable across all providers.
        usage = getattr(response, "usage", None)
        if usage:
            p = int(getattr(usage, "prompt_tokens", 0) or 0)
            c = int(getattr(usage, "completion_tokens", 0) or 0)
            try:
                cost = float(litellm.completion_cost(completion_response=response, model=model))
            except Exception:
                cost = 0.0
            self._accumulate(model, p, c, cost, _latency_ms, self._current_stage,
                             truncated=(_finish_reason == "length"))

        return response.choices[0].message.content or ""

    def _accumulate(
        self, model: str, prompt_tokens: int, completion_tokens: int,
        cost_usd: float, latency_ms: float, stage: str, truncated: bool = False,
    ) -> None:
        """Thread-safe accumulation of one LLM call's usage into the run counters.

        Safe to call from the event loop (LLMClient._call) or from worker threads
        (the DeepEval judge wrappers, via record_external_usage).
        """
        with self._counter_lock:
            self._token_totals["prompt"]     += prompt_tokens
            self._token_totals["completion"] += completion_tokens
            self._token_totals["calls"]      += 1
            self._token_totals["cost_usd"]   += cost_usd
            self._token_totals["latency_ms"] += latency_ms
            if truncated:
                self._token_totals["truncated_calls"] += 1
            self._models_used[model] = self._models_used.get(model, 0) + 1
            st = self._stage_totals.setdefault(
                stage or "unknown", {"calls": 0, "total_tokens": 0, "cost_usd": 0.0}
            )
            st["calls"]        += 1
            st["total_tokens"] += prompt_tokens + completion_tokens
            st["cost_usd"]     += cost_usd

    def record_external_usage(
        self, model: str, prompt_tokens: int, completion_tokens: int,
        cost_usd: float = 0.0, latency_ms: float = 0.0, stage: Optional[str] = None,
    ) -> None:
        """Record usage from an LLM call made OUTSIDE this client (e.g. the DeepEval
        judge metrics), so the LLMOps token summary includes evaluation/judge spend.

        Attributed to the current pipeline stage unless `stage` is given. Thread-safe.
        """
        self._accumulate(
            model,
            int(prompt_tokens or 0),
            int(completion_tokens or 0),
            float(cost_usd or 0.0),
            float(latency_ms or 0.0),
            stage or self._current_stage,
        )

    @staticmethod
    def _extract_json(text: str) -> Any:
        if not text:
            return None
        # 1. Direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 2. Markdown code block
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass
        # 3. Greedy brace/bracket extraction
        for start_char, end_char in [('{', '}'), ('[', ']')]:
            start = text.find(start_char)
            if start != -1:
                end = text.rfind(end_char)
                if end > start:
                    try:
                        return json.loads(text[start:end + 1])
                    except json.JSONDecodeError:
                        pass
        return None

    @staticmethod
    def _parse_retry_after(error_msg: str) -> float:
        m = re.search(r"retry.?after[:\s]+(\d+(?:\.\d+)?)", error_msg, re.I)
        if m:
            return float(m.group(1))
        m = re.search(r"(\d+(?:\.\d+)?)\s*second", error_msg, re.I)
        if m:
            return float(m.group(1))
        return 10.0   # default backoff

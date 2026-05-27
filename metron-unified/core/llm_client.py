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
import time
from typing import Any, Optional

import litellm

from .config import (
    LLM_PROVIDERS, FALLBACK_CHAIN,
    get_model, resolve_api_key, should_optimize_tokens, get_token_budget,
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
        provider_name: str = "Groq",
        api_key: str = "",
        azure_endpoint: str = "",
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
        aws_region: str = "",
        bedrock_model_id: str = "",
    ):
        self.provider_name = provider_name
        self.api_key = resolve_api_key(provider_name, api_key)
        self.azure_endpoint = azure_endpoint.strip()
        self.aws_access_key_id = aws_access_key_id.strip()
        self.aws_secret_access_key = aws_secret_access_key.strip()
        self.aws_region = aws_region.strip() or "us-east-1"
        self.bedrock_model_id = bedrock_model_id.strip()
        if provider_name not in LLM_PROVIDERS:
            print(f"[LLMClient] WARNING: Unknown provider '{provider_name}', falling back to Groq. "
                  f"Known providers: {list(LLM_PROVIDERS.keys())}")
        provider_info = LLM_PROVIDERS.get(provider_name, LLM_PROVIDERS["Groq"])
        self.rate_limiter = RateLimiter(provider_info.get("rpm", 30))
        self.optimize_tokens = should_optimize_tokens(provider_name)
        # Map model → exhausted_at timestamp (monotonic). Replaced the old set so
        # exhaustion expires after _EXHAUSTION_COOLDOWN_S instead of lasting forever.
        self._exhausted: dict[str, float] = {}

        # Token management — set by pipeline before each stage
        self._current_stage: str = "unknown"
        self._mlflow_run_id: str = ""

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

        primary_model = get_model(self.provider_name, task)
        # User-specified Bedrock model overrides the provider default
        if self.bedrock_model_id and self.provider_name == "AWS Bedrock":
            primary_model = f"bedrock/{self.bedrock_model_id}"

        candidates = [primary_model]

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
                    if self.provider_name == "Azure OpenAI":
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

        _t0 = time.monotonic()
        response = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=45)
        _latency_ms = (time.monotonic() - _t0) * 1000.0

        # Track which model served this call
        self._models_used[model] = self._models_used.get(model, 0) + 1

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
            self._token_totals["prompt"]     += p
            self._token_totals["completion"] += c
            self._token_totals["calls"]      += 1
            self._token_totals["cost_usd"]   += cost
            self._token_totals["latency_ms"] += _latency_ms
            if _finish_reason == "length":
                self._token_totals["truncated_calls"] += 1
            st = self._stage_totals.setdefault(self._current_stage, {"calls": 0, "total_tokens": 0, "cost_usd": 0.0})
            st["calls"]        += 1
            st["total_tokens"] += p + c
            st["cost_usd"]     += cost

        # Tag this span with the pipeline stage — best-effort, never blocks
        try:
            import mlflow
            active_span = mlflow.get_current_active_span()
            if active_span:
                active_span.set_attribute("pipeline.stage", self._current_stage)
        except Exception:
            pass

        return response.choices[0].message.content or ""

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

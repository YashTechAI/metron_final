"""
Custom DeepEval model wrappers for all supported LLM providers.

DeepEval's built-in metric constructors accept model= as either:
  - a plain string like "gpt-4o" (routed to OpenAI)
  - a DeepEvalBaseLLM subclass instance (custom routing)

Passing "azure/gpt-4o" or "gemini/..." fails DeepEval's internal model-name
validation. This module wraps each provider so all DeepEval metrics (GEval,
HallucinationMetric, AnswerRelevancyMetric, BiasMetric) route through the
correct backend.

Usage:
    from core.deepeval_azure import make_deepeval_model, make_deepeval_azure_model
    model = make_deepeval_model(config)          # provider-agnostic (new)
    model = make_deepeval_azure_model()          # Azure-only (legacy, unchanged)
    metric = HallucinationMetric(threshold=0.5, model=model)
"""

from __future__ import annotations
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse


def _parse_deployment(endpoint: str) -> str:
    """Extract deployment name from Azure OpenAI endpoint URL."""
    parsed = urlparse(endpoint)
    parts  = [p for p in parsed.path.split("/") if p]
    try:
        idx = parts.index("deployments")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return "gpt-4o"


def _parse_base_url(endpoint: str) -> str:
    """Extract base URL (scheme + netloc) from Azure OpenAI endpoint URL."""
    parsed = urlparse(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}/"


class _DeepEvalAzureOpenAI:
    """
    Minimal DeepEvalBaseLLM subclass that routes through Azure OpenAI.
    Defined as an inner class so the import of deepeval is deferred —
    avoids import-time side effects when deepeval is not installed.
    """
    pass


def make_deepeval_azure_model():
    """
    Build and return a DeepEvalBaseLLM-compatible Azure OpenAI model instance.
    Returns None if deepeval or openai is not installed.
    """
    try:
        from deepeval.models import DeepEvalBaseLLM
        from openai import AzureOpenAI
    except ImportError:
        return None

    endpoint   = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    api_key    = os.environ.get("AZURE_OPENAI_API_KEY", "")

    # Return None when Azure credentials are not configured — callers check
    # `if deval_model is None:` to skip DeepEval metrics and show a warning.
    # Without this guard, the model is created with empty credentials, the
    # warning never fires, and every metric call fails with an auth error.
    if not endpoint or not api_key:
        return None

    api_version= os.environ.get("AZURE_API_VERSION", "2025-01-01-preview")
    deployment = _parse_deployment(endpoint)
    base_url   = _parse_base_url(endpoint)

    class AzureGPT4oModel(DeepEvalBaseLLM):
        """Azure OpenAI GPT-4o model for DeepEval evaluation metrics."""

        def __init__(self):
            # Set attributes before super().__init__ because that calls load_model()
            self._deployment  = deployment
            self._base_url    = base_url
            self._api_key     = api_key
            self._api_version = api_version
            super().__init__(model_name=f"azure-{deployment}")

        def load_model(self):
            return AzureOpenAI(
                azure_endpoint=self._base_url,
                api_key=self._api_key,
                api_version=self._api_version,
                timeout=45.0,
                max_retries=0,
            )

        def generate(self, prompt: str) -> str:
            import time
            last_exc = None
            for attempt in range(3):
                try:
                    response = self.model.chat.completions.create(
                        model=self._deployment,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0,
                        max_tokens=2048,
                    )
                    return response.choices[0].message.content
                except Exception as e:
                    last_exc = e
                    msg = str(e)
                    if "429" in msg or "rate_limit" in msg.lower() or "rate limit" in msg.lower():
                        wait = 10 * (attempt + 1)
                        print(f"[DeepEval/Azure] Rate limited — waiting {wait}s (attempt {attempt+1}/3)")
                        time.sleep(wait)
                    else:
                        raise
            raise RuntimeError(f"DeepEval Azure: max retries exceeded — {last_exc}")

        async def a_generate(self, prompt: str) -> str:
            import asyncio
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.generate, prompt)

        def get_model_name(self) -> str:
            return f"Azure/{self._deployment}"

    return AzureGPT4oModel()


# ── Generic LiteLLM-backed wrapper ────────────────────────────────────────────

def _make_deepeval_litellm_model(model_name: str, litellm_kwargs: Dict[str, Any]):
    """
    Build a DeepEvalBaseLLM subclass that calls litellm.completion() synchronously.
    Works for any provider supported by LiteLLM (Gemini, Bedrock, Groq, NIM, etc.).
    Returns None if deepeval is not installed.
    """
    try:
        from deepeval.models import DeepEvalBaseLLM
    except ImportError:
        return None

    class _LiteLLMModel(DeepEvalBaseLLM):
        def __init__(self):
            self._model_name = model_name
            self._kwargs = litellm_kwargs
            super().__init__(model_name=model_name)

        def load_model(self):
            return None   # stateless — litellm handles the connection

        def generate(self, prompt: str) -> str:
            import time
            import litellm
            last_exc = None
            for attempt in range(3):
                try:
                    response = litellm.completion(
                        model=self._model_name,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0,
                        max_tokens=2048,
                        **self._kwargs,
                    )
                    return response.choices[0].message.content or ""
                except Exception as e:
                    last_exc = e
                    msg = str(e)
                    if "429" in msg or "rate_limit" in msg.lower() or "rate limit" in msg.lower():
                        wait = 10 * (attempt + 1)
                        print(f"[DeepEval/{model_name}] Rate limited — waiting {wait}s (attempt {attempt+1}/3)")
                        time.sleep(wait)
                    else:
                        raise
            raise RuntimeError(f"DeepEval {model_name}: max retries exceeded — {last_exc}")

        async def a_generate(self, prompt: str) -> str:
            import asyncio
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.generate, prompt)

        def get_model_name(self) -> str:
            return self._model_name

    return _LiteLLMModel()


def make_deepeval_model(config=None):
    """
    Provider-agnostic factory: returns the right DeepEvalBaseLLM for config.llm_provider.

    Dispatch:
      Azure      → make_deepeval_azure_model() (native AzureOpenAI client, unchanged)
      Gemini     → LiteLLM wrapper (gemini/gemini-2.5-flash)
      AWS Bedrock→ LiteLLM wrapper (bedrock/claude-3-5-sonnet) + AWS credentials
      Others     → LiteLLM wrapper (Groq / NIM) with provider API key

    Returns None when deepeval is not installed or credentials are missing.
    """
    if config is None:
        return make_deepeval_azure_model()

    provider = (getattr(config, "llm_provider", "") or "").lower()

    if "azure" in provider:
        return make_deepeval_azure_model()

    if "gemini" in provider or "google" in provider:
        api_key = getattr(config, "llm_api_key", "") or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            return None
        return _make_deepeval_litellm_model(
            "gemini/gemini-2.5-flash",
            {"api_key": api_key},
        )

    if "bedrock" in provider or "aws" in provider:
        aws_key    = getattr(config, "aws_access_key_id", "")    or os.environ.get("AWS_ACCESS_KEY_ID", "")
        aws_secret = getattr(config, "aws_secret_access_key", "") or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        aws_region = getattr(config, "aws_region", "")            or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        if not aws_key or not aws_secret:
            return None
        model_id = (getattr(config, "bedrock_model_id", "") or "").strip() or "anthropic.claude-3-5-sonnet-20241022-v2:0"
        return _make_deepeval_litellm_model(
            f"bedrock/{model_id}",
            {
                "aws_access_key_id":     aws_key,
                "aws_secret_access_key": aws_secret,
                "aws_region_name":       aws_region,
            },
        )

    # Groq / NVIDIA NIM / others — use the primary model key
    from core.config import get_model, LLM_PROVIDERS
    model_name = get_model(getattr(config, "llm_provider", "Groq"), "judge")
    api_key    = getattr(config, "llm_api_key", "") or ""
    provider_info = LLM_PROVIDERS.get(getattr(config, "llm_provider", "Groq"), {})
    if not api_key:
        env_key = provider_info.get("env_key", "")
        api_key = os.environ.get(env_key, "") if env_key else ""
    if not api_key:
        return None

    prefix = model_name.split("/")[0] if "/" in model_name else ""
    kwargs: Dict[str, Any] = {}
    if prefix == "groq":
        kwargs["api_key"] = api_key
    elif prefix == "nvidia_nim":
        kwargs["api_key"] = api_key
        kwargs["api_base"] = "https://integrate.api.nvidia.com/v1"

    return _make_deepeval_litellm_model(model_name, kwargs)

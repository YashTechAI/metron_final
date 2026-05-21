"""
MLflow run lifecycle manager for METRON pipeline runs.

All MLflow I/O runs in a ThreadPoolExecutor — never blocks the async event loop.
Handles concurrent pipeline runs safely via explicit run_id (not thread-local context).
Token data is captured automatically by mlflow.litellm.autolog() and read back via
MlflowClient at the end of each run.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mlflow-run")
_TRACKING_URI = ""
_EXPERIMENT_NAME = "metron-llmops"


def configure(tracking_uri: str, experiment_name: str = "metron-llmops") -> None:
    global _TRACKING_URI, _EXPERIMENT_NAME
    _TRACKING_URI = tracking_uri
    _EXPERIMENT_NAME = experiment_name


def is_enabled() -> bool:
    return bool(_TRACKING_URI)


def setup_autolog() -> None:
    """
    Enable mlflow.litellm.autolog() globally.
    Must be called once at server startup AFTER configure().
    After this call, every litellm.acompletion() is auto-instrumented:
      - input_tokens, output_tokens, total_tokens recorded per call
      - latency_ms recorded per call
      - cost estimate recorded per call
    Prompt/response text is NOT logged (privacy).
    """
    if not _TRACKING_URI:
        return
    try:
        import mlflow
        mlflow.set_tracking_uri(_TRACKING_URI)
        mlflow.set_experiment(_EXPERIMENT_NAME)
        # No-argument call is safe across all MLflow 3.x versions.
        # Autolog creates UI traces in the MLflow Traces tab (bonus visibility).
        # Token counts for METRON's LLMOps tab are read directly from
        # response.usage in LLMClient._call() — not from autolog metrics.
        mlflow.litellm.autolog()
        print(f"[MLflow] litellm autolog active → {_TRACKING_URI}")
    except Exception as e:
        print(f"[MLflow] autolog setup failed (non-fatal): {e}")


def start_run(
    metron_run_id: str,
    project_id: str,
    domain: str,
    provider: str,
    num_personas: int,
) -> Optional[str]:
    """
    Creates a new MLflow run and returns its mlflow_run_id.
    Returns None if MLflow is not configured or on any error.
    Blocks briefly (up to 10s) — called once at pipeline start.
    """
    if not _TRACKING_URI:
        return None
    future = _EXECUTOR.submit(
        _do_start_run, metron_run_id, project_id, domain, provider, num_personas
    )
    try:
        return future.result(timeout=10)
    except Exception as e:
        print(f"[MLflow] start_run failed (non-fatal): {e}")
        return None


def end_run(mlflow_run_id: Optional[str], status: str = "FINISHED") -> None:
    """Fire-and-forget — terminates the MLflow run in background."""
    if not _TRACKING_URI or not mlflow_run_id:
        return
    _EXECUTOR.submit(_do_end_run, mlflow_run_id, status)


def set_stage_tag(mlflow_run_id: Optional[str], stage: str) -> None:
    """Fire-and-forget — updates the current stage tag on the MLflow run."""
    if not _TRACKING_URI or not mlflow_run_id:
        return
    _EXECUTOR.submit(_do_set_tag, mlflow_run_id, "metron.current_stage", stage)


def log_token_metrics(
    mlflow_run_id: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    calls: int,
    cost_usd: float,
    latency_ms: float,
    by_stage: dict,
    retry_count: int = 0,
    truncated_calls: int = 0,
    models_used: Optional[dict] = None,
    pipeline_duration_s: float = 0.0,
) -> None:
    """
    Write final pipeline token totals into the MLflow run as first-class metrics.
    Blocking — called once at pipeline end, guarantees write completes before
    read_token_summary() is called so MLflow is the authoritative data source.
    """
    if not _TRACKING_URI or not mlflow_run_id:
        return
    future = _EXECUTOR.submit(
        _do_log_token_metrics,
        mlflow_run_id, prompt_tokens, completion_tokens,
        total_tokens, calls, cost_usd, latency_ms, by_stage,
        retry_count, truncated_calls, models_used or {}, pipeline_duration_s,
    )
    try:
        future.result(timeout=10)
    except Exception as e:
        print(f"[MLflow] log_token_metrics failed (non-fatal): {e}")


def read_token_summary(mlflow_run_id: Optional[str]) -> Optional[Dict]:
    """
    Reads aggregated token metrics from the MLflow run.
    Token data is written by log_token_metrics() at pipeline end.
    Blocks up to 10s — called once after log_token_metrics() completes.
    Returns None if not available or MLflow is not configured.
    """
    if not _TRACKING_URI or not mlflow_run_id:
        return None
    future = _EXECUTOR.submit(_do_read_summary, mlflow_run_id)
    try:
        return future.result(timeout=10)
    except Exception as e:
        print(f"[MLflow] read_token_summary failed (non-fatal): {e}")
        return None


# ── Internal helpers (run in executor thread, synchronous MLflow calls) ────────

def _do_start_run(
    metron_run_id: str,
    project_id: str,
    domain: str,
    provider: str,
    num_personas: int,
) -> str:
    import mlflow

    mlflow.set_tracking_uri(_TRACKING_URI)
    mlflow.set_experiment(_EXPERIMENT_NAME)

    run_name = f"{project_id[:8]}::{domain}::{time.strftime('%Y-%m-%dT%H:%M')}"
    run = mlflow.start_run(run_name=run_name)
    mlflow_run_id = run.info.run_id

    mlflow.set_tags({
        "metron.run_id":     metron_run_id,
        "metron.project_id": project_id,
        "metron.domain":     domain,
        "metron.provider":   provider,
    })
    mlflow.log_params({
        "domain":       domain,
        "provider":     provider,
        "num_personas": str(num_personas),
    })

    # End the context-manager-based run so the thread-local active run is clear.
    # The run stays in RUNNING state in the DB; we close it explicitly via
    # set_terminated() in _do_end_run() later.
    mlflow.end_run()
    return mlflow_run_id


def _do_end_run(mlflow_run_id: str, status: str) -> None:
    import mlflow
    from mlflow.entities import RunStatus

    mlflow.set_tracking_uri(_TRACKING_URI)
    client = mlflow.tracking.MlflowClient()
    terminal_status = (
        RunStatus.to_string(RunStatus.FINISHED)
        if status == "FINISHED"
        else RunStatus.to_string(RunStatus.FAILED)
    )
    try:
        client.set_terminated(mlflow_run_id, status=terminal_status)
    except Exception as e:
        print(f"[MLflow] set_terminated failed (non-fatal): {e}")


def _do_set_tag(mlflow_run_id: str, key: str, value: str) -> None:
    import mlflow

    mlflow.set_tracking_uri(_TRACKING_URI)
    try:
        mlflow.tracking.MlflowClient().set_tag(mlflow_run_id, key, value)
    except Exception as e:
        print(f"[MLflow] set_tag failed (non-fatal): {e}")


def _do_log_token_metrics(
    mlflow_run_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    calls: int,
    cost_usd: float,
    latency_ms: float,
    by_stage: dict,
    retry_count: int = 0,
    truncated_calls: int = 0,
    models_used: dict = None,
    pipeline_duration_s: float = 0.0,
) -> None:
    """
    Write token totals into the MLflow run as first-class run metrics.
    Runs in executor thread. MLflow becomes the authoritative token store —
    read_token_summary() reads these exact keys back.
    """
    import mlflow

    mlflow.set_tracking_uri(_TRACKING_URI)
    client = mlflow.tracking.MlflowClient()

    avg_latency    = round(latency_ms / calls, 1) if calls else 0.0
    # TPOT — total pipeline latency divided by total output tokens
    tpot_ms        = round(latency_ms / completion_tokens, 2) if completion_tokens else 0.0
    # Token Efficiency — how much output per unit of input
    token_eff      = round(completion_tokens / prompt_tokens, 4) if prompt_tokens else 0.0
    # TPM Velocity — tokens per minute over the full pipeline run
    tpm_velocity   = round((total_tokens / pipeline_duration_s) * 60, 1) if pipeline_duration_s else 0.0
    # Truncation Rate — fraction of calls where max_tokens was hit
    truncation_rate = round(truncated_calls / calls, 4) if calls else 0.0

    # Aggregate metrics — readable from run.data.metrics
    metrics: Dict[str, float] = {
        "prompt_tokens":        float(prompt_tokens),
        "completion_tokens":    float(completion_tokens),
        "total_tokens":         float(total_tokens),
        "total_calls":          float(calls),
        "cost":                 round(cost_usd, 6),
        "avg_latency_ms":       avg_latency,
        "tpot_ms":              tpot_ms,
        "token_efficiency":     token_eff,
        "tpm_velocity":         tpm_velocity,
        "retry_count":          float(retry_count),
        "truncated_calls":      float(truncated_calls),
        "truncation_rate":      truncation_rate,
    }

    # Per-stage metrics — stage.<name>.tokens / .calls / .cost
    for stage, s in by_stage.items():
        metrics[f"stage.{stage}.tokens"] = float(s.get("total_tokens", 0))
        metrics[f"stage.{stage}.calls"]  = float(s.get("calls", 0))
        metrics[f"stage.{stage}.cost"]   = float(s.get("cost_usd", 0.0))

    try:
        import json as _json
        from mlflow.entities import Metric as _Metric
        _ts = int(time.time() * 1000)
        metric_objects = [_Metric(key=k, value=v, timestamp=_ts, step=0) for k, v in metrics.items()]
        client.log_batch(mlflow_run_id, metrics=metric_objects)
        # Store model names as a tag — avoids encoding issues with /, ., - in metric keys
        if models_used:
            client.set_tag(mlflow_run_id, "metron.models_used", _json.dumps(models_used))
        print(f"[MLflow] Token metrics logged → run {mlflow_run_id[:8]} | "
              f"{total_tokens} tokens, {calls} calls, TPOT={tpot_ms:.1f}ms, eff={token_eff:.3f}")
    except Exception as e:
        print(f"[MLflow] log_metrics failed (non-fatal): {e}")


def _do_read_summary(mlflow_run_id: str) -> Dict:
    """
    Read token data back from the MLflow run metrics written by _do_log_token_metrics.
    MLflow is the source of truth — the LLMOps tab shows exactly what MLflow stores.
    """
    import mlflow

    mlflow.set_tracking_uri(_TRACKING_URI)
    client = mlflow.tracking.MlflowClient()
    run    = client.get_run(mlflow_run_id)
    m      = run.data.metrics   # all metrics logged by _do_log_token_metrics

    total_prompt     = int(m.get("prompt_tokens", 0))
    total_completion = int(m.get("completion_tokens", 0))
    total_tokens     = int(m.get("total_tokens", 0))
    total_calls      = int(m.get("total_calls", 0))
    estimated_cost   = float(m.get("cost", 0.0))
    avg_latency      = float(m.get("avg_latency_ms", 0.0))

    # Reconstruct per-stage breakdown from stage.<name>.* metric keys
    by_stage: Dict[str, Dict] = {}
    for key, val in m.items():
        if not key.startswith("stage."):
            continue
        parts = key.split(".")          # ["stage", "<name>", "<metric>"]
        if len(parts) != 3:
            continue
        _, stage_name, metric = parts
        s = by_stage.setdefault(stage_name, {"calls": 0, "total_tokens": 0, "cost_usd": 0.0})
        if metric == "tokens":
            s["total_tokens"] = int(val)
        elif metric == "calls":
            s["calls"] = int(val)
        elif metric == "cost":
            s["cost_usd"] = float(val)

    # Read model names from tag — stored as JSON to preserve original model name format
    import json as _json
    models_used: Dict[str, int] = {}
    try:
        tag_val = run.data.tags.get("metron.models_used", "")
        if tag_val:
            models_used = _json.loads(tag_val)
    except Exception:
        pass

    return {
        "total_calls":             total_calls,
        "total_prompt_tokens":     total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens":            total_tokens,
        "estimated_cost_usd":      round(estimated_cost, 6),
        "avg_latency_ms":          avg_latency,
        "tpot_ms":                 round(float(m.get("tpot_ms", 0.0)), 2),
        "token_efficiency_ratio":  round(float(m.get("token_efficiency", 0.0)), 4),
        "tpm_velocity":            round(float(m.get("tpm_velocity", 0.0)), 1),
        "retry_count":             int(m.get("retry_count", 0)),
        "truncated_calls":         int(m.get("truncated_calls", 0)),
        "truncation_rate":         round(float(m.get("truncation_rate", 0.0)), 4),
        "models_used":             models_used,
        "by_stage":                by_stage,
        "mlflow_run_id":           mlflow_run_id,
    }

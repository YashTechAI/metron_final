"""
MLflow run lifecycle manager for METRON pipeline runs.

All MLflow I/O runs in a ThreadPoolExecutor — never blocks the async event loop.
Handles concurrent pipeline runs safely via explicit run_id (not thread-local context).
Token metrics are computed in Python and logged to MLflow as first-class metrics;
they are also persisted to metron_runs.db so the LLMOps tab survives server restarts.
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
    Prompt/response text is NOT logged (privacy).
    """
    if not _TRACKING_URI:
        return
    try:
        import mlflow
        mlflow.set_tracking_uri(_TRACKING_URI)
        mlflow.set_experiment(_EXPERIMENT_NAME)
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
    Blocking — guarantees write completes before read_token_summary() is called.
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
    Reads aggregated token metrics back from the MLflow run.
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

    # End context-manager run so thread-local active run is clear.
    # The run stays RUNNING in the DB; closed explicitly via set_terminated() later.
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
    import mlflow

    mlflow.set_tracking_uri(_TRACKING_URI)
    client = mlflow.tracking.MlflowClient()

    avg_latency     = round(latency_ms / calls, 1) if calls else 0.0
    tpot_ms         = round(latency_ms / completion_tokens, 2) if completion_tokens else 0.0
    token_eff       = round(completion_tokens / prompt_tokens, 4) if prompt_tokens else 0.0
    tpm_velocity    = round((total_tokens / pipeline_duration_s) * 60, 1) if pipeline_duration_s else 0.0
    truncation_rate = round(truncated_calls / calls, 4) if calls else 0.0

    metrics: Dict[str, float] = {
        "prompt_tokens":     float(prompt_tokens),
        "completion_tokens": float(completion_tokens),
        "total_tokens":      float(total_tokens),
        "total_calls":       float(calls),
        "cost":              round(cost_usd, 6),
        "avg_latency_ms":    avg_latency,
        "tpot_ms":           tpot_ms,
        "token_efficiency":  token_eff,
        "tpm_velocity":      tpm_velocity,
        "retry_count":       float(retry_count),
        "truncated_calls":   float(truncated_calls),
        "truncation_rate":   truncation_rate,
    }

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
        # Store model names as tag — preserves /, ., - in model name strings
        if models_used:
            client.set_tag(mlflow_run_id, "metron.models_used", _json.dumps(models_used))
        print(f"[MLflow] Metrics logged → run {mlflow_run_id[:8]} | "
              f"{total_tokens} tokens, {calls} calls, TPOT={tpot_ms:.1f}ms")
    except Exception as e:
        print(f"[MLflow] log_batch failed (non-fatal): {e}")


def _do_read_summary(mlflow_run_id: str) -> Dict:
    import mlflow

    mlflow.set_tracking_uri(_TRACKING_URI)
    client = mlflow.tracking.MlflowClient()
    run = client.get_run(mlflow_run_id)
    m   = run.data.metrics

    by_stage: Dict[str, Dict] = {}
    for key, val in m.items():
        if not key.startswith("stage."):
            continue
        parts = key.split(".")
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

    import json as _json
    models_used: Dict[str, int] = {}
    try:
        tag_val = run.data.tags.get("metron.models_used", "")
        if tag_val:
            models_used = _json.loads(tag_val)
    except Exception:
        pass

    return {
        "total_calls":             int(m.get("total_calls", 0)),
        "total_prompt_tokens":     int(m.get("prompt_tokens", 0)),
        "total_completion_tokens": int(m.get("completion_tokens", 0)),
        "total_tokens":            int(m.get("total_tokens", 0)),
        "estimated_cost_usd":      round(float(m.get("cost", 0.0)), 6),
        "avg_latency_ms":          round(float(m.get("avg_latency_ms", 0.0)), 1),
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

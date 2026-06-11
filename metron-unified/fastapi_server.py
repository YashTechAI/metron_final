"""
FastAPI server — unified METRON backend.
8 endpoints: all 7 from existing backend (backward-compatible) + new /api/parse-document.
Runs pipeline.py as a background task, stores jobs in-memory.
"""

from __future__ import annotations
import asyncio
import os
import uuid
from typing import Any, Dict, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import get_current_user
from core.config import CORS_ORIGINS, LLM_PROVIDERS
from core.llm_client import LLMClient
from core.models import (
    ApplicationType, ConnectTestRequest, JobStatus,
    ParseDocumentRequest, PreviewRequest, RunConfig,
)
from core.adapters.chatbot import ChatbotAdapter
from core import db as _db
import core.mlflow_run as _mlflow_run
from pipeline import run_pipeline
from stages.s0_profile.document_parser import parse_document
from stages.s0_profile.architecture_parser import parse_architecture_text, parse_architecture_image
from stages.s1_personas.fishbone_builder import build_slots
from stages.s1_personas.persona_builder import build_all_personas

app = FastAPI(title="METRON Unified API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store — keyed by run_id
# Fix 22: stores only status/progress/results (NO llm_api_key or auth_token)
jobs: Dict[str, Dict[str, Any]] = {}


_PIPELINE_TIMEOUT_MINUTES = 90


async def _reap_stuck_jobs():
    """Background task: mark jobs stuck in running/queued for >90 min as failed."""
    from datetime import datetime as _dt, timedelta
    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        try:
            cutoff = _dt.utcnow() - timedelta(minutes=_PIPELINE_TIMEOUT_MINUTES)
            for run_id, job in list(jobs.items()):
                if job.get("status") not in ("running", "queued"):
                    continue
                ts_str = job.get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    if _dt.fromisoformat(ts_str) < cutoff:
                        msg = f"Run automatically stopped after {_PIPELINE_TIMEOUT_MINUTES} minutes."
                        job["status"]  = "failed"
                        job["error"]   = msg
                        job["message"] = f"Timed out ({_PIPELINE_TIMEOUT_MINUTES} min limit)"
                        print(f"[Reaper] Timed out run {run_id}")
                        try:
                            _db.mark_run_failed(run_id, msg)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception as e:
            print(f"[Reaper] Error: {e}")


@app.on_event("startup")
async def _startup():
    """Init DB and re-populate in-memory jobs from recent completed/failed runs."""
    _mlflow_run.configure(
        tracking_uri=os.environ.get("MLFLOW_TRACKING_URI", ""),
        experiment_name=os.environ.get("MLFLOW_EXPERIMENT_NAME", "metron-llmops"),
    )
    _mlflow_run.setup_autolog()
    try:
        _db.init_db()
        for row in _db.load_recent_jobs(hours=24):
            run_id = row["run_id"]
            is_failed = row["status"] == "failed"
            jobs[run_id] = {
                "status":        row["status"],
                "progress":      100,
                "message":       "Failed (recovered from DB)" if is_failed else "Completed (recovered from DB)",
                "current_phase": "",
                "phase_results": {},
                "log_events":    [],
                "error":         row.get("results", {}).get("error") if is_failed else None,
                "results":       None if is_failed else row.get("results"),
                "user_email":    row.get("user_email", ""),
                "project_id":    row.get("project_id", ""),
                "eval_warnings": [],
                "token_summary": row.get("token_summary"),
            }
        print(f"[DB] Recovered {len(jobs)} recent runs from SQLite on startup.")
        asyncio.create_task(_reap_stuck_jobs())
    except Exception as e:
        print(f"[DB] Startup recovery failed (non-fatal): {e}")


def _check_job_ownership(job: Dict, user_email: str) -> None:
    """Raise 403 if the job has an owner and it doesn't match the requesting user."""
    owner = job.get("user_email", "")
    if owner and owner != user_email:
        raise HTTPException(status_code=403, detail="Access denied: this run belongs to another user")

# ──────────────────────────────────────────────────────────────────────────
# GET /api/providers — list LLM providers
# ──────────────────────────────────────────────────────────────────────────
@app.get("/api/providers")
async def get_providers():
    return {
        name: {
            "description":      info["description"],
            "rpm":              info["rpm"],
            "models":           info["models"],
            "default":          info["default"],
            "env_key":          info["env_key"],
            "token_optimize":   info.get("token_optimize", False),
            "selectable_models": info.get("selectable_models", []),
        }
        for name, info in LLM_PROVIDERS.items()
    }


# ──────────────────────────────────────────────────────────────────────────
# GET /api/tools/status — check optional tool availability
# ──────────────────────────────────────────────────────────────────────────
@app.get("/api/tools/status")
async def get_tools_status():
    loop = asyncio.get_event_loop()

    def _check(pkg: str) -> bool:
        import sys
        if pkg in sys.modules:
            return True
        try:
            __import__(pkg)
            return True
        except ImportError:
            return False
        except Exception:
            return False

    async def _safe_check(pkg: str) -> bool:
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, _check, pkg),
                timeout=5.0,
            )
        except Exception:
            return False

    presidio_ok, detoxify_ok, llmguard_ok, deepeval_ok, ragas_ok = await asyncio.gather(
        _safe_check("presidio_analyzer"),
        _safe_check("detoxify"),
        _safe_check("llm_guard"),
        _safe_check("deepeval"),
        _safe_check("ragas"),
    )

    return {
        "presidio": {
            "installed": presidio_ok,
            "description": "PII detection (Presidio — replaces LLM PII guessing)",
            "used_for": "pii_leakage metric in security evaluation",
        },
        "detoxify": {
            "installed": detoxify_ok,
            "description": "Toxicity classifier (Detoxify — replaces LLM toxicity scoring)",
            "used_for": "toxicity metric in security evaluation",
        },
        "llm_guard": {
            "installed": llmguard_ok,
            "description": "Prompt injection scanner (LLM Guard — replaces LLM injection guessing)",
            "used_for": "prompt_injection metric in security evaluation",
        },
        "deepeval": {
            "installed": deepeval_ok,
            "description": "Structured LLM evaluation (DeepEval — GEval, Hallucination, Bias, Relevancy)",
            "used_for": "hallucination + answer_relevancy in functional; geval in quality; bias in security",
        },
        "ragas": {
            "installed": ragas_ok,
            "description": "RAG evaluation framework (RAGAS — structural faithfulness, no LLM)",
            "used_for": "ragas_faithfulness in quality evaluation (RAG mode only)",
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# POST /api/connect-test — test chatbot endpoint connectivity
# ──────────────────────────────────────────────────────────────────────────
@app.post("/api/connect-test")
async def connect_test(req: ConnectTestRequest, request: Request):
    get_current_user(request)
    adapter = ChatbotAdapter(
        endpoint_url=req.endpoint_url,
        request_field=req.request_field,
        response_field=req.response_field,
        auth_type=req.auth_type,
        auth_token=req.auth_token,
        request_template=req.request_template,
        response_trim_marker=req.response_trim_marker,
    )
    success, message = await adapter.test_connection()
    return {"success": success, "message": message}


# ──────────────────────────────────────────────────────────────────────────
# POST /api/parse-document — NEW: seed doc → AppProfile
# ──────────────────────────────────────────────────────────────────────────
@app.post("/api/parse-document")
async def parse_document_endpoint(req: ParseDocumentRequest, request: Request):
    get_current_user(request)
    if not req.document_text.strip():
        raise HTTPException(400, "document_text is required")
    if not _has_credentials(req):
        raise HTTPException(400, f"API key required for {req.llm_provider}")

    llm_client = LLMClient(
        req.llm_provider, req.llm_api_key,
        azure_endpoint=req.azure_endpoint,
        aws_access_key_id=getattr(req, "aws_access_key_id", "") or "",
        aws_secret_access_key=getattr(req, "aws_secret_access_key", "") or "",
        aws_region=getattr(req, "aws_region", "") or "",
        bedrock_model_id=getattr(req, "bedrock_model_id", "") or "",
    )
    profile = await parse_document(req.document_text, llm_client)
    return {
        "application_type":  profile.application_type.value,
        "domain":            profile.domain,
        "user_types":        profile.user_types,
        "use_cases":         profile.use_cases,
        "domain_vocabulary": profile.domain_vocabulary,
        "boundaries":        profile.boundaries,
        "success_criteria":  profile.success_criteria,
        "agents":            [a.model_dump() for a in profile.agents],
    }


# ──────────────────────────────────────────────────────────────────────────
# POST /api/parse-architecture — extract structured fields from doc or image
# ──────────────────────────────────────────────────────────────────────────
@app.post("/api/parse-architecture")
async def parse_architecture_endpoint(
    request:        Request,
    content:        str           = Form(""),
    image:          Optional[UploadFile] = File(None),
    llm_provider:   str           = Form("Groq"),
    llm_api_key:    str           = Form(""),
    azure_endpoint: str           = Form(""),
):
    get_current_user(request)
    """
    Parse an architecture document (text) or diagram (image) and return
    structured architecture fields ready to populate the configure form.

    - content: raw text from an uploaded .txt / .pdf / .md document
    - image:   an uploaded image file (PNG / JPG / WEBP) of an architecture diagram
    """
    import base64

    if not content.strip() and not image:
        raise HTTPException(400, "Provide either text content or an image file")

    llm_client = LLMClient(llm_provider, llm_api_key, azure_endpoint=azure_endpoint)
    # Note: parse_architecture_image endpoint uses direct form params, not a RunConfig.
    # AWS Bedrock credentials would need dedicated form params if required here.

    if image:
        raw_bytes  = await image.read()
        b64_data   = base64.b64encode(raw_bytes).decode("utf-8")
        mime_type  = image.content_type or "image/png"
        result     = await parse_architecture_image(b64_data, mime_type, llm_client)
    else:
        result = await parse_architecture_text(content, llm_client)

    return result


# ──────────────────────────────────────────────────────────────────────────
# POST /api/preview — generate personas + scenarios
# ──────────────────────────────────────────────────────────────────────────
@app.post("/api/preview")
async def preview(req: PreviewRequest, request: Request):
    get_current_user(request)
    if not req.agent_description.strip():
        raise HTTPException(400, "agent_description is required")
    if not _has_credentials(req):
        raise HTTPException(400, f"API key required for {req.llm_provider}")

    llm_client = LLMClient(
        req.llm_provider, req.llm_api_key,
        azure_endpoint=req.azure_endpoint,
        aws_access_key_id=getattr(req, "aws_access_key_id", "") or "",
        aws_secret_access_key=getattr(req, "aws_secret_access_key", "") or "",
        aws_region=getattr(req, "aws_region", "") or "",
        bedrock_model_id=getattr(req, "bedrock_model_id", "") or "",
    )

    from stages.s0_profile.document_parser import build_profile_from_config
    profile = build_profile_from_config(
        agent_description=req.agent_description,
        agent_domain=req.agent_domain,
        application_type_str=req.application_type,
    )

    # Fishbone slots → personas
    slots    = build_slots(profile, num_personas=req.num_personas)
    personas = await build_all_personas(slots, profile, llm_client)

    # Generate scenarios (functional prompts as scenarios for UI compatibility)
    from stages.s2_tests.functional_gen import generate_functional_prompts
    scenarios = []
    for p in personas[:req.num_scenarios]:
        prompts = await generate_functional_prompts(p, profile, llm_client)
        for pr in prompts[:2]:
            scenarios.append({
                "id":               pr.prompt_id,
                "name":             p.name,
                "description":      f"{p.goal[:80]} · Test for {p.expertise.value} {p.user_type}",
                "initial_prompt":   pr.text,
                "expected_behavior": pr.expected_behavior or "",
                "category":         "functional",
            })

    return {
        "personas": [
            {
                "id":             p.persona_id,
                "name":           p.name,
                "description":    p.description or p.background[:200],
                "traits":         p.traits,
                "sample_prompts": p.sample_prompts or p.entry_points,
                "fishbone":       p.fishbone_dimensions,
                "expertise":      p.expertise.value,
                "emotional_state": p.emotional_state.value,
                "intent":         p.intent.value,
            }
            for p in personas
        ],
        "scenarios": scenarios,
    }


# ──────────────────────────────────────────────────────────────────────────
# POST /api/run — submit test job
# ──────────────────────────────────────────────────────────────────────────
@app.post("/api/run")
async def run_tests(
    request: Request,
    background_tasks: BackgroundTasks,
    config: str = Form(...),
    document: Optional[UploadFile] = File(None),
    ground_truth_file: Optional[UploadFile] = File(None),
):
    user = get_current_user(request)
    import json, csv, io
    try:
        config_data = json.loads(config)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid config JSON")

    # Parse ground truth file (CSV or JSON) into list of {question, expected_answer, context}
    if ground_truth_file:
        try:
            raw = (await ground_truth_file.read()).decode("utf-8", errors="ignore")
            filename = ground_truth_file.filename or ""
            if filename.endswith(".json"):
                parsed = json.loads(raw)

                # ── Locate the list of Q&A pairs regardless of JSON shape ──────
                # Supported shapes:
                #   1. Root array:          [{"question": ...}, ...]
                #   2. Root object/wrapper: {"test_cases": [...]} or
                #                          {"data": [...]} or
                #                          {"questions": [...]} etc.
                rows = None
                if isinstance(parsed, list):
                    rows = parsed
                elif isinstance(parsed, dict):
                    # Recursive search for a list of Q&A pairs
                    def find_list(obj):
                        if isinstance(obj, list):
                            return obj
                        if isinstance(obj, dict):
                            for wrapper_key in (
                                "test_cases", "cases", "questions", "data",
                                "items", "entries", "records", "samples",
                                "ground_truth", "qa_pairs", "pairs",
                            ):
                                if wrapper_key in obj and isinstance(obj[wrapper_key], list):
                                    return obj[wrapper_key]
                            for v in obj.values():
                                found = find_list(v)
                                if found:
                                    return found
                        return None
                    rows = find_list(parsed)

                if rows:
                    pairs = []
                    for r in rows:
                        if not isinstance(r, dict):
                            continue
                        # Question — accept multiple field names
                        q = (
                            r.get("question") or r.get("query") or
                            r.get("q") or r.get("input") or
                            r.get("user_input") or r.get("prompt") or ""
                        )
                        # Expected answer — accept multiple field names
                        a = (
                            r.get("expected_answer") or r.get("answer") or
                            r.get("a") or r.get("reference") or
                            r.get("expected_output") or r.get("ground_truth") or ""
                        )
                        if q and a:
                            pairs.append({
                                "question":        str(q).strip(),
                                "expected_answer": str(a).strip(),
                            })
                    config_data["ground_truth"] = pairs
                    print(f"[API] Parsed {len(pairs)} ground truth pairs from JSON ({filename})")
                else:
                    print(f"[API] Could not locate a list of Q&A pairs in JSON file: {filename}")

            else:
                # CSV: flexible header — map common column name variants
                reader = csv.DictReader(io.StringIO(raw))
                pairs = []
                for row in reader:
                    q = (
                        row.get("question") or row.get("query") or
                        row.get("q") or row.get("input") or
                        row.get("user_input") or ""
                    )
                    a = (
                        row.get("expected_answer") or row.get("answer") or
                        row.get("a") or row.get("reference") or
                        row.get("ground_truth") or ""
                    )
                    if q and a:
                        pairs.append({
                            "question":        q.strip(),
                            "expected_answer": a.strip(),
                        })
                config_data["ground_truth"] = pairs
                print(f"[API] Parsed {len(pairs)} ground truth pairs from CSV ({filename})")

        except Exception as e:
            print(f"[API] Could not parse ground truth file: {e}")

    run_config = RunConfig(**config_data)

    if not _has_credentials(run_config):
        raise HTTPException(400, f"API key required for {run_config.llm_provider}")

    # Read uploaded document
    doc_text = ""
    if document:
        try:
            content = await document.read()
            doc_text = content.decode("utf-8", errors="ignore")
        except Exception:
            doc_text = ""

    # ── Quota gate — check BEFORE creating the job ────────────────────────
    quota_ok, quota_reason = _db.try_consume_quota(user["email"])
    if not quota_ok:
        raise HTTPException(status_code=429, detail=quota_reason)

    run_id = str(uuid.uuid4())

    # Fix 29: project_id comes from config (set by UI from dashboard [id]) or defaults to run_id
    project_id = run_config.project_id or run_id

    from datetime import datetime as _dt
    # Job store contains NO API keys — only status/progress/config summary
    jobs[run_id] = {
        "status":        "queued",
        "progress":      0,
        "message":       "Queued",
        "current_phase": "",
        "phase_results": {},
        "log_events":    [],
        "eval_warnings": [],
        "error":         None,
        "results":       None,
        "project_id":    project_id,
        "user_email":    user["email"],
        "timestamp":     _dt.utcnow().isoformat(),
        # Safe config summary (no credentials)
        "config_summary": {
            "endpoint_url":    run_config.endpoint_url,
            "agent_domain":    run_config.agent_domain,
            "llm_provider":    run_config.llm_provider,
            "application_type": run_config.application_type.value,
        },
    }

    background_tasks.add_task(
        run_pipeline,
        run_id=run_id,
        config=run_config,
        job_store=jobs,
        doc_text=doc_text,
        project_id=project_id,
        user_email=user["email"],
        user_role=user.get("role", "all"),
    )

    return {"run_id": run_id, "project_id": project_id}


# ──────────────────────────────────────────────────────────────────────────
# GET /api/job/{run_id}/status — poll job status
# ──────────────────────────────────────────────────────────────────────────
@app.get("/api/job/{run_id}/status")
async def get_job_status(run_id: str, request: Request):
    user = get_current_user(request)
    job = jobs.get(run_id)
    if not job:
        # Fall back to DB for runs that completed before last restart
        db_row = _db.get_run(run_id)
        if db_row:
            owner = db_row.get("user_email", "")
            if owner and owner != user["email"]:
                raise HTTPException(403, "Access denied: this run belongs to another user")
            return {
                "run_id":        run_id,
                "status":        db_row.get("status", "completed"),
                "progress":      100,
                "message":       "Completed (from DB)",
                "current_phase": "",
                "phase_results": {},
                "log_events":    [],
                "eval_warnings": [],
                "error":         None,
            }
        raise HTTPException(404, "Job not found")
    _check_job_ownership(job, user["email"])
    return {
        "run_id":        run_id,
        "status":        job["status"],
        "progress":      job["progress"],
        "message":       job["message"],
        "current_phase": job["current_phase"],
        "phase_results": job["phase_results"],
        "log_events":    job.get("log_events", []),
        "eval_warnings": job.get("eval_warnings", []),
        "error":         job["error"],
    }


# ──────────────────────────────────────────────────────────────────────────
# GET /api/job/{run_id}/results — fetch completed results
# ──────────────────────────────────────────────────────────────────────────
@app.get("/api/job/{run_id}/results")
async def get_job_results(run_id: str, request: Request):
    user = get_current_user(request)
    job = jobs.get(run_id)
    if not job:
        # Fall back to DB
        db_row = _db.get_run(run_id)
        if db_row:
            owner = db_row.get("user_email", "")
            if owner and owner != user["email"]:
                raise HTTPException(403, "Access denied: this run belongs to another user")
            if db_row.get("results"):
                return db_row["results"]
        raise HTTPException(404, "Job not found")
    _check_job_ownership(job, user["email"])
    if job["status"] in ("running", "queued"):
        raise HTTPException(202, "Job still running")
    if job["status"] == "failed":
        raise HTTPException(500, job.get("error", "Pipeline failed"))
    return job["results"]


# ──────────────────────────────────────────────────────────────────────────
# GET /api/job/{run_id}/token-summary — LLMOps: token usage for a run
# ──────────────────────────────────────────────────────────────────────────
@app.get("/api/job/{run_id}/token-summary")
async def get_token_summary(run_id: str, request: Request):
    user = get_current_user(request)
    job = jobs.get(run_id)

    # Try in-memory first, then fall back to DB (survives server restarts)
    if job:
        _check_job_ownership(job, user["email"])
        summary = job.get("token_summary")
    else:
        summary = _db.get_token_summary(run_id)

    if summary and summary.get("total_tokens", 0) > 0:
        return summary

    return {"total_calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}


# ──────────────────────────────────────────────────────────────────────────
# GET /api/health
# ──────────────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


# ──────────────────────────────────────────────────────────────────────────
# GET /api/job/{run_id}/status — also checks DB if not in memory (Fix 21)
# (Replaces the original endpoint above with DB fallback)
# ──────────────────────────────────────────────────────────────────────────

# NOTE: The original GET /api/job/{run_id}/status endpoint stays at line 393+
# as-is. We add DB fallback there via an override at import time.
# Actually we patch it here:

# ──────────────────────────────────────────────────────────────────────────
# GET /api/project/{project_id}/runs — Fix 28: run history for a project
# ──────────────────────────────────────────────────────────────────────────
@app.get("/api/project/{project_id}/runs")
async def get_project_runs(project_id: str, request: Request):
    user = get_current_user(request)
    """Return all runs for a project: DB rows + any in-memory runs not yet persisted."""
    # Verify the project belongs to the requesting user
    project = _db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if project.get("user_email") != user["email"]:
        raise HTTPException(403, "Not your project")
    try:
        db_runs = _db.get_runs_for_project(project_id)
        db_run_ids = {r["run_id"] for r in db_runs}

        # Include in-memory runs not yet saved to DB (e.g. if save_run failed)
        mem_runs = []
        for rid, job in jobs.items():
            if job.get("project_id") != project_id or rid in db_run_ids:
                continue
            # Skip runs that belong to a different user
            if job.get("user_email") and job["user_email"] != user["email"]:
                continue
            results = job.get("results") or {}
            mem_runs.append({
                "run_id":           rid,
                "project_id":       project_id,
                "timestamp":        job.get("timestamp", ""),
                "health_score":     results.get("health_score"),
                "domain":           job.get("config_summary", {}).get("agent_domain", ""),
                "application_type": job.get("config_summary", {}).get("application_type", ""),
                "status":           job["status"],
            })

        all_runs = sorted(db_runs + mem_runs, key=lambda r: r.get("timestamp", ""), reverse=True)
        return {"project_id": project_id, "runs": all_runs}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")


# ──────────────────────────────────────────────────────────────────────────
# GET /api/runs/{run_id_a}/compare/{run_id_b} — Fix 28: regression diff
# ──────────────────────────────────────────────────────────────────────────
@app.get("/api/runs/{run_id_a}/compare/{run_id_b}")
async def compare_runs(run_id_a: str, run_id_b: str, request: Request):
    user = get_current_user(request)
    """Compare health scores and class pass-rates between two runs."""
    # Ownership check — allow if user_email is absent (legacy rows pre-fix)
    for rid in (run_id_a, run_id_b):
        job = jobs.get(rid)
        if job:
            _check_job_ownership(job, user["email"])
        else:
            db_row = _db.get_run(rid)
            if db_row:
                owner = db_row.get("user_email", "")
                if owner and owner != user["email"]:
                    raise HTTPException(403, f"Access denied to run {rid}")
    try:
        diff = _db.compare_runs(run_id_a, run_id_b)
        if "error" in diff:
            raise HTTPException(404, diff["error"])
        return diff
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Compare error: {e}")


# ──────────────────────────────────────────────────────────────────────────
# Auth endpoints — login/logout handled by AWS Cognito on the frontend.
# /api/auth/me validates the Cognito Bearer token and returns the caller's email.
# ──────────────────────────────────────────────────────────────────────────

@app.post("/api/auth/logout")
async def auth_logout():
    return JSONResponse({"ok": True})


@app.get("/api/auth/me")
async def auth_me(request: Request):
    return get_current_user(request)


# ── Project persistence endpoints ───────────────────────────────────────────

class _ProjectBody(BaseModel):
    project_id: str
    name: str
    endpoint: str
    api_key: str = ""
    document_text: str = ""
    document_name: str = ""


@app.post("/api/projects")
async def create_project(body: _ProjectBody, request: Request):
    user = get_current_user(request)
    _db.save_project(
        body.project_id, user["email"], body.name, body.endpoint,
        body.api_key, body.document_text, body.document_name,
    )
    return {"ok": True}


@app.get("/api/projects")
async def list_projects(request: Request):
    user = get_current_user(request)
    projects = _db.get_projects_for_user(user["email"])
    return {"projects": projects}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, request: Request):
    get_current_user(request)
    project = _db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    user = get_current_user(request)
    project = _db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.get("user_email") != user["email"]:
        raise HTTPException(status_code=403, detail="Not your project")
    _db.delete_project(project_id)
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────
# GET /api/quota  — current user's quota status
# ──────────────────────────────────────────────────────────────────────────
@app.get("/api/quota")
async def get_quota(request: Request):
    user = get_current_user(request)
    status = _db.get_quota_status(user["email"])
    status["email"] = user["email"]
    return status


# ──────────────────────────────────────────────────────────────────────────
# Tenant Admin endpoints  —  /api/admin/*
# All require role = tenant_admin or super_admin
# ──────────────────────────────────────────────────────────────────────────

def _require_tenant_admin(user: dict) -> None:
    if user.get("role") not in ("tenant_admin", "super_admin"):
        raise HTTPException(403, "Tenant admin access required")


def _require_super_admin(user: dict) -> None:
    if user.get("role") != "super_admin":
        raise HTTPException(403, "Super admin access required")


@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    user = get_current_user(request)
    _require_tenant_admin(user)
    result = _db.get_tenant_stats(user["tenant_id"])
    result["admin_email"] = user["email"]
    return result


@app.get("/api/admin/runs")
async def admin_list_runs(request: Request):
    user = get_current_user(request)
    _require_tenant_admin(user)
    return {"runs": _db.get_tenant_runs(user["tenant_id"])}


@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    user = get_current_user(request)
    _require_tenant_admin(user)
    return {"users": _db.get_tenant_users(user["tenant_id"])}


class _AddUserBody(BaseModel):
    user_email: str
    role: str = "viewer"
    run_limit: int = 10


_KNOWN_PHASES = {"functional", "security", "quality", "performance", "load"}

def _validate_role(role: str) -> bool:
    """Accept legacy named roles AND any '+'-joined combination of known phase names."""
    legacy = {"functional_tester", "security_tester", "quality", "performance", "load",
              "security+functional", "functional+quality", "performance+load", "all"}
    if role in legacy:
        return True
    # Dynamic checkbox role: every part must be a known phase name
    parts = {p.strip() for p in role.split("+")}
    return bool(parts) and parts.issubset(_KNOWN_PHASES)


@app.post("/api/admin/users")
async def admin_add_user(body: _AddUserBody, request: Request):
    from core.cognito_admin import invite_user
    user = get_current_user(request)
    _require_tenant_admin(user)
    if not _validate_role(body.role):
        raise HTTPException(400, "Invalid role. Use phase names (functional, security, quality, performance, load) joined by '+'.")
    _db.add_user_to_tenant(body.user_email, user["tenant_id"], body.role, body.run_limit)
    result = invite_user(body.user_email)
    if not result["ok"]:
        print(f"[Admin] Cognito invite failed for {body.user_email}: {result.get('error')} (user added to DB anyway)")
    return {"ok": True, "invite_sent": result["ok"]}


class _UpdateUserBody(BaseModel):
    run_limit: Optional[int] = None
    role: Optional[str] = None


@app.put("/api/admin/users/{email}")
async def admin_update_user(email: str, body: _UpdateUserBody, request: Request):
    user = get_current_user(request)
    _require_tenant_admin(user)
    if body.run_limit is not None:
        if not _db.update_user_limit(email, body.run_limit, user["tenant_id"]):
            raise HTTPException(404, "User not found in your tenant")
    if body.role is not None:
        if not _validate_role(body.role):
            raise HTTPException(400, "Invalid role. Use phase names (functional, security, quality, performance, load) joined by '+'.")
        if not _db.update_user_role(email, body.role, user["tenant_id"]):
            raise HTTPException(404, "User not found in your tenant")
    return {"ok": True}


@app.delete("/api/admin/users/{email}")
async def admin_remove_user(email: str, request: Request):
    from core.cognito_admin import delete_user
    user = get_current_user(request)
    _require_tenant_admin(user)
    if not _db.remove_user(email, user["tenant_id"]):
        raise HTTPException(404, "User not found in your tenant")
    delete_user(email)
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────
# Super Admin endpoints  —  /api/super/*
# All require role = super_admin
# ──────────────────────────────────────────────────────────────────────────

@app.get("/api/super/tenants")
async def super_list_tenants(request: Request):
    user = get_current_user(request)
    _require_super_admin(user)
    return {"tenants": _db.get_all_tenants()}


class _CreateTenantBody(BaseModel):
    name: str
    quota_limit: int = 50


@app.post("/api/super/tenants")
async def super_create_tenant(body: _CreateTenantBody, request: Request):
    user = get_current_user(request)
    _require_super_admin(user)
    tenant = _db.create_tenant(body.name, body.quota_limit)
    return tenant


class _UpdateTenantBody(BaseModel):
    name: Optional[str] = None
    quota_limit: Optional[int] = None


@app.put("/api/super/tenants/{tenant_id}")
async def super_update_tenant(tenant_id: str, body: _UpdateTenantBody, request: Request):
    user = get_current_user(request)
    _require_super_admin(user)
    if not _db.update_tenant(tenant_id, body.name, body.quota_limit):
        raise HTTPException(404, "Tenant not found")
    return {"ok": True}


@app.get("/api/super/tenants/{tenant_id}")
async def super_get_tenant(tenant_id: str, request: Request):
    user = get_current_user(request)
    _require_super_admin(user)
    detail = _db.get_tenant_detail(tenant_id)
    if not detail:
        raise HTTPException(404, "Tenant not found")
    return detail


@app.post("/api/super/tenants/{tenant_id}/reset-quota")
async def super_reset_tenant_quota(tenant_id: str, request: Request):
    user = get_current_user(request)
    _require_super_admin(user)
    if not _db.reset_tenant_quota(tenant_id):
        raise HTTPException(404, "Tenant not found")
    return {"ok": True}


@app.post("/api/super/tenants/{tenant_id}/users")
async def super_add_user_to_tenant(tenant_id: str, body: _AddUserBody, request: Request):
    """Super admin can add/move any user into any tenant and invite them via Cognito."""
    from core.cognito_admin import invite_user
    user = get_current_user(request)
    _require_super_admin(user)
    _db.add_user_to_tenant(body.user_email, tenant_id, body.role, body.run_limit)
    result = invite_user(body.user_email)
    if not result["ok"]:
        print(f"[Super] Cognito invite failed for {body.user_email}: {result.get('error')}")
    return {"ok": True, "invite_sent": result["ok"]}


@app.delete("/api/super/tenants/{tenant_id}/users/{email}")
async def super_remove_user_from_tenant(tenant_id: str, email: str, request: Request):
    """Remove a user from a tenant. If removing a tenant_admin, cascade-delete all their tenant's users."""
    from core.cognito_admin import delete_user
    user = get_current_user(request)
    _require_super_admin(user)

    # Check if the user being removed is a tenant_admin — if so, delete all users in the tenant
    target = _db.get_user(email)
    if target and target.get("role") == "tenant_admin":
        all_users = _db.get_tenant_users(tenant_id)
        for u in all_users:
            _db.remove_user(u["user_email"], tenant_id)
            delete_user(u["user_email"])
    else:
        _db.remove_user(email, tenant_id)
        delete_user(email)
    return {"ok": True}


@app.delete("/api/super/tenants/{tenant_id}")
async def super_delete_tenant(tenant_id: str, request: Request):
    """Delete an entire tenant — removes all users from Cognito and DB, then deletes the tenant."""
    from core.cognito_admin import delete_user
    user = get_current_user(request)
    _require_super_admin(user)
    all_users = _db.get_tenant_users(tenant_id)
    for u in all_users:
        _db.remove_user(u["user_email"], tenant_id)
        delete_user(u["user_email"])
    if not _db.delete_tenant(tenant_id):
        raise HTTPException(404, "Tenant not found")
    return {"ok": True}


# ── Helpers ────────────────────────────────────────────────────────────────
def _env_key_set(provider_name: str) -> bool:
    env_key = LLM_PROVIDERS.get(provider_name, {}).get("env_key", "")
    return bool(env_key and os.environ.get(env_key))


def _has_credentials(req) -> bool:
    """Return True when the request carries sufficient credentials for its provider.

    AWS Bedrock uses aws_access_key_id/aws_secret_access_key instead of llm_api_key,
    so we accept either inline AWS creds or the usual API-key / env-var path.
    """
    provider = getattr(req, "llm_provider", "") or ""
    if "bedrock" in provider.lower() or "aws" in provider.lower():
        inline_aws = bool(
            getattr(req, "aws_access_key_id", "") and
            getattr(req, "aws_secret_access_key", "")
        )
        return inline_aws or bool(
            os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")
        )
    return bool(getattr(req, "llm_api_key", "")) or _env_key_set(provider)

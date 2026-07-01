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
from pydantic import BaseModel

from core.auth import get_current_user
from core.config import CORS_ORIGINS, get_llm_model, get_llm_api_key, apply_llm_env
from core.llm_client import LLMClient
from core.models import (
    ApplicationType, ConnectTestRequest, PreviewRequest, RunConfig,
)
from core.adapters.chatbot import ChatbotAdapter
from core.nia_agent import (
    apply_nia_agent_config, nia_request_template,
    NIA_A2A_REQUEST_FIELD, NIA_A2A_RESPONSE_FIELD, NIA_A2A_RESPONSE_TRIM_MARKER,
)
from core import db as _db
from pipeline import run_pipeline
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
    try:
        apply_llm_env()   # bridge LLM_API_KEY → provider env var (e.g. GEMINI_API_KEY)
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
                "organization_id": row.get("organization_id", ""),
                "project_id":    row.get("project_id", ""),
                "eval_warnings": [],
                "token_summary": row.get("token_summary"),
            }
        print(f"[DB] Recovered {len(jobs)} recent runs from Postgres on startup.")
        asyncio.create_task(_reap_stuck_jobs())
    except Exception as e:
        print(f"[DB] Startup recovery failed (non-fatal): {e}")


def _can_access(row_org: str, row_email: str, user: Dict) -> bool:
    """Org-scoped access rule for projects and runs.

    When both the resource and the caller carry an organization_id, access is granted to
    anyone in the same organization. Otherwise (legacy rows with no org, or no-org tokens
    / local-dev bypass) fall back to creator-email matching so org-less resources are never
    shared beyond their owner.
    """
    user_org = user.get("organization_id", "")
    if user_org and row_org:
        return row_org == user_org
    return bool(row_email) and row_email == user.get("email", "")


def _check_job_ownership(job: Dict, user: Dict) -> None:
    """Raise 403 if the requesting user is not in the run's organization (or its creator)."""
    if not _can_access(job.get("organization_id", ""), job.get("user_email", ""), user):
        raise HTTPException(status_code=403, detail="Access denied: this run belongs to another organization")

# ──────────────────────────────────────────────────────────────────────────
# GET /api/providers — the single env-configured LLM (model + whether key is set)
# ──────────────────────────────────────────────────────────────────────────
@app.get("/api/providers")
async def get_providers():
    return {
        "model":       get_llm_model(),
        "configured":  _has_credentials(),
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
    user = get_current_user(request)
    # NIA agent mode: only endpoint_url comes from the UI; the A2A template,
    # response path and trim marker are fixed server-side, and the caller's JWT
    # (from the Authorization header) is injected into the request body. This
    # exercises the exact same path a real run uses.
    adapter = ChatbotAdapter(
        endpoint_url=req.endpoint_url,
        request_field=NIA_A2A_REQUEST_FIELD,
        response_field=NIA_A2A_RESPONSE_FIELD,
        auth_type="none",
        auth_token="",
        request_template=nia_request_template(),
        response_trim_marker=NIA_A2A_RESPONSE_TRIM_MARKER,
        injected_token=user.get("access_token", ""),
    )
    success, message = await adapter.test_connection()
    return {"success": success, "message": message}


# ──────────────────────────────────────────────────────────────────────────
# POST /api/parse-architecture — extract structured fields from doc or image
# ──────────────────────────────────────────────────────────────────────────
@app.post("/api/parse-architecture")
async def parse_architecture_endpoint(
    request:        Request,
    content:        str           = Form(""),
    image:          Optional[UploadFile] = File(None),
):
    user = get_current_user(request)
    """
    Parse an architecture document (text) or diagram (image) and return
    structured architecture fields ready to populate the configure form.

    - content: raw text from an uploaded .txt / .pdf / .md document
    - image:   an uploaded image file (PNG / JPG / WEBP) of an architecture diagram
    """
    import base64

    if not content.strip() and not image:
        raise HTTPException(400, "Provide either text content or an image file")

    # Per-org LLM config (org id from JWT); falls back to .env when no org / NIA DB.
    llm_client = LLMClient(organization_id=user.get("organization_id", ""))

    if image:
        raw_bytes  = await image.read()
        b64_data   = base64.b64encode(raw_bytes).decode("utf-8")
        mime_type  = image.content_type or "image/png"
        result     = await parse_architecture_image(b64_data, mime_type, llm_client)
    else:
        result = await parse_architecture_text(content, llm_client)

    return result


# ──────────────────────────────────────────────────────────────────────────
# POST /api/extract-document — extract text from an uploaded seed doc (PDF/DOCX/text)
# ──────────────────────────────────────────────────────────────────────────
@app.post("/api/extract-document")
async def extract_document(request: Request, file: UploadFile = File(...)):
    """Extract plain text from an uploaded seed/knowledge document.

    Browsers can only read plain text via FileReader; PDF/DOCX are extracted here
    (pypdf / python-docx) so the existing projects.document_text storage and LLM
    profile extraction keep working unchanged.
    """
    get_current_user(request)
    data = await file.read()
    from core.file_text import extract_file_text
    text = extract_file_text(file.filename or "", data)
    if not text.strip():
        raise HTTPException(
            400,
            "Could not extract any text from this file. For PDFs, make sure it is a "
            "text-based PDF (not a scanned image).",
        )
    return {"text": text, "name": file.filename or "document"}


# ──────────────────────────────────────────────────────────────────────────
# POST /api/preview — generate personas + scenarios
# ──────────────────────────────────────────────────────────────────────────
@app.post("/api/preview")
async def preview(req: PreviewRequest, request: Request):
    user = get_current_user(request)
    if not _has_credentials():
        raise HTTPException(400, "No LLM configured: set up this org in the NIA DB, or set LLM_MODEL / LLM_API_KEY")

    # Per-org LLM config (org id from JWT); falls back to .env when no org / NIA DB.
    llm_client = LLMClient(organization_id=user.get("organization_id", ""))

    # Profile source priority:
    #   1. The project's stored seed document (projects.document_text) — primary path,
    #      extracted (and cached) into a rich AppProfile, same as the run pipeline.
    #   2. agent_description fallback — legacy / projects without a seed document.
    profile = None
    if req.project_id:
        proj = _db.get_project(req.project_id)
        seed_text = (proj or {}).get("document_text") or ""
        if seed_text.strip():
            from core.profile_cache import get_or_build_profiles
            profile, _tech = await get_or_build_profiles(
                seed_text, llm_client, project_id=req.project_id, project=proj,
            )
            # Domain dropdown stays an optional override (mirrors the run pipeline).
            if req.agent_domain:
                profile.domain = req.agent_domain.lower()

    if profile is None:
        if not req.agent_description.strip():
            raise HTTPException(400, "Provide a project_id with a seed document, or an agent_description")
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
    # Org id from the JWT selects this org's LLM config from the NIA DB (per-org).
    run_config.organization_id = user.get("organization_id", "")

    # NIA agent mode: the target adapter config is fixed server-side (not from the
    # UI) and the caller's JWT is forwarded into the A2A request body. Overrides
    # whatever the client sent for endpoint template / response path / auth.
    apply_nia_agent_config(run_config, user.get("access_token", ""))

    if not _has_credentials(run_config):
        raise HTTPException(400, "No LLM configured: set up this org in the NIA DB, or set LLM_MODEL / LLM_API_KEY")

    # Read uploaded document (legacy/override path — a one-off doc just for this run).
    # Uses the shared extractor so an uploaded PDF/DOCX override also works here.
    doc_text = ""
    if document:
        try:
            content = await document.read()
            from core.file_text import extract_file_text
            doc_text = extract_file_text(document.filename or "", content)
        except Exception:
            doc_text = ""

    # Seed-document path (default): when no doc is uploaded for this run, use the
    # project's stored seed document so prompts are generated from it without re-upload.
    # The pipeline's stage 0 already extracts a rich profile + technical attack surface
    # from doc_text; here we just make sure doc_text is the stored seed document.
    if not doc_text.strip() and run_config.project_id:
        try:
            _proj = _db.get_project(run_config.project_id)
            if _proj and _proj.get("document_text"):
                doc_text = _proj["document_text"]
        except Exception as _seed_err:
            print(f"[API] Could not load stored seed document for project "
                  f"{run_config.project_id}: {_seed_err}")

    # Authorization removed — no quota gating. Every authenticated user may run.
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
        "organization_id": run_config.organization_id,
        "timestamp":     _dt.utcnow().isoformat(),
        # Safe config summary (no credentials)
        "config_summary": {
            "endpoint_url":    run_config.endpoint_url,
            "agent_domain":    run_config.agent_domain,
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
            if not _can_access(db_row.get("organization_id", ""), db_row.get("user_email", ""), user):
                raise HTTPException(403, "Access denied: this run belongs to another organization")
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
    _check_job_ownership(job, user)
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
            if not _can_access(db_row.get("organization_id", ""), db_row.get("user_email", ""), user):
                raise HTTPException(403, "Access denied: this run belongs to another organization")
            if db_row.get("results"):
                return db_row["results"]
        raise HTTPException(404, "Job not found")
    _check_job_ownership(job, user)
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
        _check_job_ownership(job, user)
        summary = job.get("token_summary")
    else:
        # Gate the DB fallback the same way the status/results endpoints do.
        db_row = _db.get_run(run_id)
        if db_row and not _can_access(db_row.get("organization_id", ""), db_row.get("user_email", ""), user):
            raise HTTPException(403, "Access denied: this run belongs to another organization")
        summary = _db.get_token_summary(run_id)

    if summary and summary.get("total_tokens", 0) > 0:
        return summary

    return {"total_calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}


# ──────────────────────────────────────────────────────────────────────────
# DELETE /api/job/{run_id} — delete a single run from history (org-wide)
# ──────────────────────────────────────────────────────────────────────────
@app.delete("/api/job/{run_id}")
async def delete_run(run_id: str, request: Request):
    user = get_current_user(request)
    # Resolve the run from memory or DB for the access check (404 if it exists nowhere).
    job = jobs.get(run_id)
    if job:
        row_org, row_email = job.get("organization_id", ""), job.get("user_email", "")
    else:
        db_row = _db.get_run(run_id)
        if not db_row:
            raise HTTPException(404, "Run not found")
        row_org, row_email = db_row.get("organization_id", ""), db_row.get("user_email", "")
    if not _can_access(row_org, row_email, user):
        raise HTTPException(403, "Access denied: this run belongs to another organization")
    _db.delete_run(run_id)
    jobs.pop(run_id, None)
    return {"ok": True}


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
    # Verify the project is in the requesting user's organization
    project = _db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not _can_access(project.get("organization_id", ""), project.get("user_email", ""), user):
        raise HTTPException(403, "Not your organization's project")
    try:
        db_runs = _db.get_runs_for_project(project_id)
        db_run_ids = {r["run_id"] for r in db_runs}

        # Include in-memory runs not yet saved to DB (e.g. if save_run failed)
        mem_runs = []
        for rid, job in jobs.items():
            if job.get("project_id") != project_id or rid in db_run_ids:
                continue
            # Skip runs outside the caller's organization
            if not _can_access(job.get("organization_id", ""), job.get("user_email", ""), user):
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
# Auth — login/logout are handled by the host platform's Keycloak.
# /api/auth/me validates the injected Keycloak Bearer token and returns the
# caller's identity ({email, role, organization_id}). There is no authorization
# gating — any valid token is allowed. This is the single identity endpoint.
# ──────────────────────────────────────────────────────────────────────────

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
        organization_id=user.get("organization_id", ""),
    )
    return {"ok": True}


@app.get("/api/projects")
async def list_projects(request: Request):
    user = get_current_user(request)
    projects = _db.get_projects_visible(user.get("organization_id", ""), user["email"])
    return {"projects": projects}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, request: Request):
    user = get_current_user(request)
    project = _db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # Scope to the caller's organization — also prevents leaking the stored api_key.
    if not _can_access(project.get("organization_id", ""), project.get("user_email", ""), user):
        raise HTTPException(status_code=403, detail="Not your organization's project")
    return project


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    user = get_current_user(request)
    project = _db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # Org-wide: any member of the project's organization may delete it (and its runs).
    if not _can_access(project.get("organization_id", ""), project.get("user_email", ""), user):
        raise HTTPException(status_code=403, detail="Not your organization's project")
    _db.delete_project(project_id)
    return {"ok": True}


# ── Helpers ────────────────────────────────────────────────────────────────
def _has_credentials(req=None) -> bool:
    """True when an LLM can be resolved for this request.

    Two sources, in priority order:
      1. Per-org config from the NIA DB — assumed available when NIA is configured
         AND the request carries an organization_id (resolved per-org at call time).
      2. .env LLM_MODEL / LLM_API_KEY (local-dev fallback; Bedrock uses AWS_* env).
    """
    from core.nia_connection import is_configured
    if is_configured() and getattr(req, "organization_id", ""):
        return True
    model = get_llm_model()
    if not model:
        return False
    if model.split("/", 1)[0] == "bedrock":
        return bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))
    return bool(get_llm_api_key())

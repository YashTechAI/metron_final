# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.

## What METRON is

METRON is an AI-agent testing platform. A user describes (or uploads a seed
document for) a target AI agent, points METRON at its live endpoint, and METRON
runs an 8-stage pipeline that auto-generates personas and test prompts, drives
real conversations against the target, evaluates the responses (functional /
security / quality / RAG / performance / load), aggregates a health score, and
produces a root-cause analysis and HTML report.

The repo holds two deployables:

- **`metron-unified/`** — Python FastAPI backend: the pipeline, evaluators, LLM
  client, auth, and persistence. This is where almost all logic lives.
- **`metron-ai/`** — Next.js 16 / React 19 frontend (App Router, Tailwind v4).
  A thin client over the backend's REST API.

## Backend architecture (`metron-unified/`)

### The pipeline is the spine

[pipeline.py](metron-unified/pipeline.py) — `run_pipeline()` is the single
orchestrator. It runs as a FastAPI background task, mutating an in-memory
`job_store[run_id]` dict at each step (progress %, message, `log_events` feed)
that the frontend polls. Read this file first to understand control flow — it
wires every stage together in order. Stages live under
[stages/](metron-unified/stages/) named `s0_profile` … `s8_rca` and are imported
directly by the pipeline:

- **s0 profile** — parse seed doc / build profile from config; second pass
  extracts a 24-category technical attack surface (`technical_extractor.py` +
  `core/attack_surface_mapper.py`).
- **s1 personas** — fishbone slot matrix → LLM-built personas → coverage validation.
- **s2 tests** — generate functional, security/adversarial, and quality-criteria prompts.
- **s3 execution** — `conversation_runner.py` drives multi-turn conversations against the target.
- **s4 evaluation** — `functional`, `security`, `quality`, `rag`, `performance`,
  `load`, `garak_eval` — run largely in parallel.
- **s5 aggregation** — `aggregator.py` builds the `AggregatedReport` + health score.
- **s7 report** — `report_generator.py` → JSON + HTML.
- **s8 rca** — `rca_mapper.py` root-cause analysis + `prompt_classifier.py` per-prompt failure taxonomy.

(s6 feedback exists but is not on the main path.)

### Cross-cutting `core/`

- [core/models.py](metron-unified/core/models.py) — **the shared vocabulary.**
  All Pydantic models (`RunConfig`, `AppProfile`, `Persona`, `Conversation`,
  `MetricResult`, `AggregatedReport`, enums). Almost every stage imports from
  here; change models carefully.
- [core/llm_client.py](metron-unified/core/llm_client.py) — `LLMClient` wraps
  **litellm** with a token-bucket `RateLimiter` (20% headroom), provider
  fallback on 429/quota, JSON-extraction retries, per-stage token accounting,
  and Azure token-optimization. All LLM calls go through it.
- [core/config.py](metron-unified/core/config.py) — `LLM_PROVIDERS` registry
  (NVIDIA NIM, Azure OpenAI, Groq, Gemini, …) and `.env`-based model/key resolution.
- [core/dynamic_config.py](metron-unified/core/dynamic_config.py) +
  [core/nia_connection.py](metron-unified/core/nia_connection.py) — **per-org LLM
  config.** In production each org's provider/model/key is resolved from the
  platform's NIA Postgres DB (`application_llm_config`), keyed by `organization_id`
  (from the JWT) + `METRON_APPLICATION_NAME`, with KMS-decrypted secrets and a TTL
  cache. Falls back to `.env` `LLM_MODEL`/`LLM_API_KEY` for local dev.
- [core/db.py](metron-unified/core/db.py) — Postgres persistence for run history,
  project configs, token summaries (SSL forced). Server can recover recent runs on startup.
- [core/auth.py](metron-unified/core/auth.py) — Keycloak RS256 JWT verification
  only (no authorization: any valid token = full access). The host gateway appends
  a `$YashUnified2025$` marker to the bearer token — the real JWT is the part
  before it. JWKS cached 1 hour. `METRON_AUTH_BYPASS=1` skips verification for local dev.
- [core/adapters/](metron-unified/core/adapters/) — target-endpoint adapters
  (`chatbot`, `rag`, `form`, `multiagent`). Each exposes an async `send()` that
  abstracts how to talk to the target AI's API.

### API surface

[fastapi_server.py](metron-unified/fastapi_server.py) — ~18 endpoints under
`/api/*`. Key ones: `POST /api/run` (kicks off the pipeline),
`GET /api/job/{run_id}/status` and `/results` (frontend polls these),
`/api/extract-document` + `/api/parse-architecture` (seed-doc ingestion),
`/api/preview`, `/api/projects`. Jobs live in an in-memory
`jobs` dict; completed runs persist to Postgres.

## Frontend architecture (`metron-ai/`)

Next.js **App Router** under [app/](metron-ai/app/). The core flow is a
per-project wizard: `dashboard/project/[id]/` → `configure` → `builder` →
`preview` → `run` → `results` / `analysis`. [lib/api.ts](metron-ai/lib/api.ts) is
deliberately tiny — `authFetch` just forwards cookies (`credentials: "include"`)
because the host reverse proxy injects the auth header; the frontend never
handles tokens itself.

⚠️ **This is NOT the Next.js in your training data.** Next 16 / React 19 have
breaking changes. Before writing frontend code, read the relevant guide in
`metron-ai/node_modules/next/dist/docs/` and heed deprecation notices (per
[metron-ai/AGENTS.md](metron-ai/AGENTS.md)).

## Commands

### Backend (`metron-unified/`, Python — use the repo `.venv`)

```bash
# one-time: install deps, the spaCy model Presidio needs, and ML weights
pip install -r requirements.txt
python -m spacy download en_core_web_lg
python setup_models.py          # pre-downloads detoxify / sentence-transformers (avoids cold-start hang)

# run the API (from inside metron-unified/)
uvicorn fastapi_server:app --reload --port 8000

# tests (pytest) — the backend suite is currently a single file
pytest                          # all
pytest tests/test_multiturn_chaining.py            # one file
pytest tests/test_multiturn_chaining.py::test_name # one test
```

### Frontend (`metron-ai/`)

```bash
npm install
npm run dev      # dev server on http://localhost:3000
npm run build
npm run start
npm run lint     # eslint (eslint-config-next)
```

## Conventions & gotchas

- **`.env` is the contract.** [metron-unified/.env.example](metron-unified/.env.example)
  documents every backend setting (Keycloak, NIA DB + KMS, the LLM fallback,
  Postgres, MLflow, CORS) with inline notes — read it before touching config.
- **Stages stay decoupled** through `core/models.py` types and the `LLMClient`;
  add a new evaluation phase by adding an `s4_*` module + wiring it into
  `run_pipeline()` and `_ROLE_PHASES`/`_allowed_phases` (which gates phases by
  the selected user role).
- **Failures inside the pipeline are mostly non-fatal by design** — most stages
  catch, log a warning into `job_store`/`eval_warnings`, and continue so a single
  bad evaluator doesn't sink the whole run. Preserve that pattern.
- **Per-run `asyncio.Lock` + a global semaphore (cap 10)** guard concurrent runs;
  the in-memory `job_store` is the source of truth during a run, Postgres after.
- Token/cost tracking flows through `LLMClient` accumulators → `job_store` →
  MLflow + Postgres (`save_token_summary` must run *after* `save_run`).

# Metron — Session Handoff / Context Document

> Purpose: full context of the work done to integrate **Metron** into the YASH
> "aifirstenterprise" platform (Keycloak auth, PostgreSQL, per-org LLM config from
> the NIA DB). Hand this to a new chat to continue work.
>
> ⚠️ **No real secrets are in this file.** All credentials live in `metron-unified/.env`
> (gitignored). Placeholders below show variable *names* only.

---

## 0. Latest session (2026-06-20) — Gemini "thinking" truncation fix ✅

**Symptom:** Functional test prompts came out as the generic fallback
`"Hi, I need help with <goal>."` (e.g. *"Hi, I need help with email writer agent."*)
instead of rich, persona-specific prompts.

**Root cause (confirmed by live calls against the NIA-resolved Gemini, not theory):**
`thinking_budget=0` is **silently ignored by litellm for `gemini/gemini-2.5-flash`**.
The model kept "thinking" (~1,700 reasoning tokens), overran `max_tokens`, and
**truncated the JSON** (`finish_reason="length"`). The truncated JSON failed to parse,
so `LLMClient.complete_json()` exhausted its retries and raised. That exception:
- in **persona gen** → `build_persona` catches it → `_fallback_persona` →
  `entry_points=["Hi, I need help with {goal}."]` (no `multi_turn_scenario`);
- in **functional gen** → Priority 1 skipped (fallback persona has no scenario),
  Priority 2's LLM call truncates the same way → Priority 3 emits the canned entry_point.

The `"Hi"` (vs `_assemble_persona`'s `"Hello"`) is the exact fingerprint of `_fallback_persona`.

**Evidence (same persona-sized prompt, `max_tokens=2000`):**

| Param sent | `finish_reason` | reasoning tokens | JSON parses? |
|---|---|---|---|
| `thinking_budget=0` (old code) | `length` | 1,708 | ❌ truncated |
| `thinking={"type":"disabled"}` | `length` | 1,698 | ❌ |
| no param | `length` | 1,699 | ❌ |
| **`reasoning_effort="disable"`** | `stop` | **None** | ✅ full output |

**Why GEval still scored before the fix:** the judge's output is tiny (a score + short
reason), so it fit in the ~300 tokens left after the thinking tax. Only the **large**
structured outputs (personas, functional prompt sets) overflowed and truncated.

**Fix:** swap `thinking_budget=0` → `reasoning_effort="disable"` for Gemini in both spots:
- `core/llm_client.py` `_call()` (~L349) — all pipeline generation (personas, functional, security…).
- `core/deepeval_azure.py` `make_deepeval_model()` (~L238) — the GEval/DeepEval judge (now
  robust against long-rationale truncation; also saves ~1,700 wasted reasoning tokens/call).

**Verified end-to-end** through the real `build_persona` + `generate_functional_prompts`
and `make_deepeval_model().generate()` paths against live Gemini:
- persona is no longer a fallback (`multi_turn_scenario` = 3 turns; rich, domain-specific prompts);
- functional prompts are real LLM output (even carry the designed HTML-paste artifacts);
- judge returns complete `{"score", "reason"}` JSON (`finish_reason="stop"`).

> Note: this fixes the **truncation** (the cause of the fallback). Prompt *quality* beyond
> "real vs. canned" is unchanged.

---

## 1. What Metron is

Two apps in one repo (`metron_final/`):

| Dir | Stack | Role |
|-----|-------|------|
| `metron-unified/` | FastAPI (Python), `uvicorn` | Backend — LLM eval pipeline + REST API |
| `metron-ai/` | Next.js 16 (Turbopack, App Router) | Frontend — dashboard UI |

Metron is an **AI-evaluation platform**: you point it at a target chatbot endpoint,
it generates personas + test cases, runs conversations against the target, and scores
them across phases (functional, security, quality, performance, load) using an LLM judge
(DeepEval) + tools (Presidio, Detoxify, LLM-Guard).

It is deployed **embedded behind the platform's reverse proxy**, which injects a
Keycloak Bearer token on every request.

---

## 2. Big picture of what changed this session

1. **Removed legacy multi-tenant / admin-super / quota code** (leftover from an old
   Cognito setup) — endpoints, DB tables/columns, frontend pages, role constants.
2. **Migrated the app DB from SQLite → PostgreSQL (Supabase)**.
3. **Fixed the chatbot connect-test 500 bug**.
4. **Reworked LLM configuration** through two stages:
   - (a) from UI-entered keys → single `LLM_MODEL`/`LLM_API_KEY` from `.env`;
   - (b) → **per-organization dynamic config from the platform's NIA database**
     (KMS-decrypted), with `.env` as local-dev fallback. **This is the current state.**
5. **Wired up Keycloak auth** to match the platform's real tokens (incl. the
   `$YashUnified2025$` marker), verified against the live realm.
6. **End-to-end tested** the whole stack against real infra (Supabase Postgres,
   NIA DB + AWS KMS, live Gemini, full pipeline run).

---

## 3. Current architecture (END STATE)

### 3.1 Auth flow (`core/auth.py`)
- Platform proxy injects `Authorization: Bearer <jwt>$YashUnified2025$<suffix>`.
- `get_current_user()`:
  1. If `METRON_AUTH_BYPASS=1` → returns a dev user `{email: dev@local.test,
     role: platform_admin, organization_id: $METRON_DEV_ORG_ID}` (local dev only).
  2. Else strips the `$YashUnified2025$` marker, then `verify_token()`:
     RS256 verify against Keycloak JWKS (cached 1h), checks issuer + audience
     (`account`), extracts `email` + `organization_id` (from the `custom-data` claim).
- **No authorization** — any valid realm token gets full access (`role=platform_admin`).
  The platform is the gatekeeper. (We deliberately skip the `{client_id}_client`
  resource-role check the reference middleware does.)

### 3.2 LLM config resolution (the important part)
LLM model + API key are resolved **per request**, priority order:

1. **NIA DB (production, per-org)** — when NIA is configured (`NIA_DB_HOST`/etc set)
   AND the request has an `organization_id`:
   `core/llm_client._resolve_org_config(org_id)` →
   `core/dynamic_config.fetch_llm_config_and_pricing(org_id, METRON_APPLICATION_NAME)`:
   - SQL query against NIA Postgres (`application_llm_config` + `providers_llm_config`
     + `llm_models` + `llm_providers`) → `provider_name`, `model_code`, `encrypted_config`.
   - **KMS-decrypt** `encrypted_config` (AWS KMS, boto3) → JSON holding `api_key` (+ extras).
   - Build litellm model string `f"{provider}/{model_code.lower()}"`, pop `api_key`,
     keep the rest as `extra_kwargs`. Cached 60s (`TTLCache`).
2. **`.env` fallback (local dev)** — `LLM_MODEL` + `LLM_API_KEY` when NIA not configured.

`organization_id` flows: JWT `custom-data.organization_id` → `get_current_user` →
set on `RunConfig`/`PreviewRequest`/`ParseDocumentRequest` → `LLMClient(organization_id=…)`
and `make_deepeval_model(config)`. litellm routes by the model prefix
(`gemini/…`, `groq/…`, `azure/…`, `bedrock/…`, `nvidia_nim/…`).

**Single model for all tasks** (fast/judge/balanced collapsed). For Gemini,
`thinking_budget=0` is set so "thinking" tokens don't eat the output budget.

### 3.3 App database (`core/db.py`)
- **PostgreSQL** via `psycopg2` + `ThreadedConnectionPool` (maxconn=5), `RealDictCursor`,
  `sslmode=require` (Supabase). Two tables: `runs`, `projects` (NO `tenant_id`).
- `_strip_nul()` removes NUL (`0x00`) bytes before insert (Postgres rejects them;
  uploaded PDFs read as text can contain them).
- This is **separate** from the NIA DB (different Supabase project).

---

## 4. New files created

| File | Purpose |
|------|---------|
| `metron-unified/core/nia_connection.py` | SQLAlchemy engine to the NIA DB from `NIA_DB_*` / `NIA_DATABASE_URL`. `is_configured()`, `nia_engine()`. SSL via `NIA_DB_SSLMODE` (default psycopg2 `prefer`). |
| `metron-unified/core/dynamic_config.py` | `fetch_llm_config_and_pricing(org_id, app_name)` — NIA SQL query (no pricing) + KMS decrypt + 60s `TTLCache`. Ported from the platform's reference. |
| `metron-unified/.env` | Real config (gitignored — NOT committed). |
| `HANDOFF.md` | This file. |

> Throwaway/local-only (gitignored): `metron-unified/test_auth.py`, `metron-unified/_tok.txt`.
> Also `metron-unified/test_litellm_hello.py` (platform's reference with plaintext creds) —
> **should be gitignored or deleted**, do not commit.

---

## 5. Modified files (by area)

### Backend — `metron-unified/`
- **`core/auth.py`** — Keycloak verify; `$YashUnified2025$` marker strip; extract
  `organization_id`; `METRON_AUTH_BYPASS` + `METRON_DEV_ORG_ID`; `_FULL_ACCESS_ROLE="platform_admin"`.
- **`core/config.py`** — `get_llm_model()`, `get_llm_api_key()`, `provider_from_model()`,
  `apply_llm_env()` (mirrors `LLM_API_KEY` into provider env var e.g. `GEMINI_API_KEY`);
  `get_model()`/`resolve_api_key()` now return env values; `LLM_PROVIDERS` kept as a
  prefix→behaviour lookup (rpm, token_optimize).
- **`core/llm_client.py`** — `LLMClient(organization_id=…)`; `_resolve_org_config()`
  (NIA→env); `complete()/complete_json()` use `self.model`; `extra_kwargs` merged into
  the litellm call; Gemini `thinking_budget=0`; single-model (removed `FALLBACK_CHAIN`).
- **`core/deepeval_azure.py`** — `make_deepeval_model(config)` resolves judge from NIA
  (via `config.organization_id`) else `.env`.
- **`core/db.py`** — rewritten for PostgreSQL (psycopg2 pool); `runs`+`projects` only;
  `_strip_nul()`; removed all tenant/user/quota functions.
- **`core/models.py`** — added `organization_id` to `RunConfig`, `PreviewRequest`,
  `ParseDocumentRequest`. (Legacy `llm_provider`/`llm_api_key`/`azure_*`/`aws_*`/
  `bedrock_model_id` fields kept but ignored.)
- **`core/adapters/chatbot.py`** — connect-test fix: `_build_payload` moved INSIDE the
  `try/except` (Swagger's `"string"` template no longer 500s); bearer match changed to
  `"bearer" in auth_type.lower()`.
- **`fastapi_server.py`** — `/api/quota` → `{email, role}`; `/api/providers` →
  `{model, configured}`; endpoints build `LLMClient(organization_id=user["organization_id"])`;
  `_has_credentials()` passes if NIA configured+org OR env model/key present;
  `apply_llm_env()` at startup; removed all `/api/admin/*` + `/api/super/*` endpoints;
  startup log says "Postgres".
- **`pipeline.py`** — `LLMClient(organization_id=config.organization_id)`; removed
  `tenant_admin`/`super_admin` from `_ROLE_PHASES`; `_full_run_roles = {"all"}`.
- **`stages/s7_report/report_generator.py`** — `_FULL_RUN_ROLES = {"all"}`.
- **`requirements.txt`** — added `psycopg2-binary`, `sqlalchemy`, `cachetools`.
- **`.env.example`** — documents all the env vars below (placeholders).

### Frontend — `metron-ai/`
- **Deleted**: `proxy.ts` (Next 16 middleware causing an infinite redirect loop on a
  no-longer-set `metron_session` cookie); the `app/admin/*`, `app/super/*`,
  `app/dashboard/admin/*`, `app/dashboard/super/*` pages.
- **`app/dashboard/project/[id]/configure/page.tsx`** — removed Provider dropdown,
  API Key, Azure endpoint, AWS creds, Bedrock-model picker → replaced with a read-only
  "Evaluation LLM" card (shows server-configured model); stopped sending LLM creds.
- **`app/dashboard/project/[id]/preview/page.tsx`**, **`…/run/page.tsx`**,
  **`…/results/page.tsx`**, **`app/dashboard/page.tsx`**, **`app/dashboard/layout.tsx`**,
  **`…/builder/page.tsx`** — removed `tenant_*`, `run_limit`/`runs_used`, the
  `QuotaBanner`, the org-name reads, and dead `tenant_admin`/`super_admin` role sets.
- **`app/ops/page.tsx`** — redirect target changed from deleted `/super` to `/dashboard`.

---

## 6. Environment variables (`metron-unified/.env`)

Names + purpose only. Real values are in the gitignored `.env`.

```bash
# ── Auth (Keycloak) ──────────────────────────────────────────────
METRON_AUTH_BYPASS=1                 # 1 = skip token verify (LOCAL DEV ONLY); 0 in prod
METRON_DEV_ORG_ID=<uuid>             # bypass-mode org id, so NIA path resolves locally
KEYCLOAK_ISSUER=https://iam.dev.aifirstenterprise.ai/realms/yashtech_ai
KEYCLOAK_AUDIENCE=account            # token aud[] contains "account"
# (alt to ISSUER) KEYCLOAK_URL=… + KEYCLOAK_REALM=yashtech_ai
# (optional) KEYCLOAK_JWKS_URL=…     # override if non-standard path
# NOTE: platform appends "$YashUnified2025$" to the bearer token — auth.py strips it.

# ── LLM Source 1: NIA per-org config (PRODUCTION) ────────────────
NIA_DB_HOST=<host>                   # NIA Postgres (Supabase) — separate from app DB
NIA_DB_NAME=postgres
NIA_DB_USER=<user>
NIA_DB_PASSWORD=<secret>
NIA_DB_PORT=5432
# (alt) NIA_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/db  (takes precedence)
# (optional) NIA_DB_SSLMODE=require  # default = psycopg2 "prefer"
METRON_APPLICATION_NAME=TEST_SUITE   # this app's name in application_llm_config
AWS_REGION=ap-south-1                # AWS KMS (decrypt provider config)
AWS_ACCESS_KEY_ID=<secret>
AWS_SECRET_ACCESS_KEY=<secret>
# AWS_KMS_KEY_ID_ARN=<id>            # present but NOT needed for decrypt (ciphertext embeds key)

# ── LLM Source 2: .env fallback (LOCAL DEV, when NIA not configured) ─
# LLM_MODEL=gemini/gemini-2.5-flash
# LLM_API_KEY=<secret>
# (optional) LLM_RPM=60

# ── App database (Metron's own runs/projects — PostgreSQL/Supabase) ─
DB_USER=<user>                       # e.g. postgres.<project-ref>
DB_PASSWORD=<secret>
DB_HOST=<pooler-host>
DB_PORT=5432
DB_NAME=postgres

# ── Misc ─────────────────────────────────────────────────────────
CORS_ORIGINS=
MLFLOW_TRACKING_URI=sqlite:///mlruns/mlflow.db
MLFLOW_EXPERIMENT_NAME=metron-llmops
```

Key facts captured during integration:
- **org id** lives in the JWT at `custom-data.organization_id` (UUID).
- The NIA test row used: org `f178c851-09df-42e5-805a-41914a1ca372`, app `TEST_SUITE`
  → provider `gemini`, model `gemini-2.5-flash`, key delivered KMS-encrypted.
- AWS code reads standard names (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_REGION`),
  with fallback to `AWS_ACCESS_KEY`/`AWS_SECRET_KEY`/`AWS_DEFAULT_REGION`.

---

## 7. How to run (local dev)

Backend (`metron-unified/`, venv active):
```powershell
python -m uvicorn fastapi_server:app --reload --port 8000
```
Frontend (`metron-ai/`):
```powershell
npm run dev        # must be on :3000 (CORS + /api proxy expect it)
```
Open http://localhost:3000.

⚠️ `--reload` watches `.py`, **not `.env`** — restart the backend after editing `.env`.

Two LLM modes (controlled by `.env`):
- **NIA per-org**: set `NIA_DB_*` + AWS KMS + `METRON_APPLICATION_NAME` (+ `METRON_DEV_ORG_ID`
  for bypass). Comment out `LLM_MODEL`/`LLM_API_KEY`.
- **Local fallback**: leave `NIA_DB_*` unset, set `LLM_MODEL`/`LLM_API_KEY`.

---

## 8. How to test

- **Keycloak token** (`metron-unified/`): paste a fresh token into `_tok.txt`, then
  `python test_auth.py` → expect `AUTH OK -> {email, role}`. (Tokens expire ~hourly.)
- **NIA path** (resolve org → KMS → live LLM):
  ```powershell
  python -c "import asyncio; from dotenv import load_dotenv; load_dotenv(); from core.llm_client import LLMClient; print(asyncio.run(LLMClient(organization_id='<org-uuid>').complete('Say hello', max_tokens=30)))"
  ```
- **HTTP smoke**: `GET /api/quota`, `GET /api/providers`, project POST/GET/DELETE,
  `POST /api/preview`, `POST /api/run` then poll `GET /api/job/{run_id}/status`.

---

## 9. Verification status (what's PROVEN working)

- ✅ PostgreSQL (Supabase): project CRUD round-trip; runs persist; startup recovery.
- ✅ Keycloak: `verify_token` validated a **real platform token** against the **live JWKS**
  (issuer + `aud=account` + RS256) → email; marker-strip handles `$YashUnified2025$`.
- ✅ NIA per-org config: against the **real NIA DB + AWS KMS** → resolved
  `gemini/gemini-2.5-flash` + decrypted key → **live Gemini call returned "pong"**.
- ✅ Full pipeline run e2e: generate → execute (21+ tests) → DeepEval judge → report →
  **completed, persisted to Postgres** (a run scored 117 tests, 99 passed).
- ✅ Frontend serves `/dashboard` (HTTP 200); `tsc --noEmit` passes.
- ✅ connect-test returns graceful `{success, message}` (no 500).

---

## 10. Known issues / TODO

1. **`health_score` is always `None`** (HIGH — not yet fixed). Auth issues
   `role=platform_admin`, but `_FULL_RUN_ROLES = {"all"}` in `pipeline.py` (~L783) and
   `stages/s7_report/report_generator.py` (~L41). So `_is_full_run` is False → the
   headline health score is never computed/saved. **Every production run is
   `platform_admin`**, so the overall score is always blank. Fix: add `"platform_admin"`
   to both sets, or base `_is_full_run` on whether all phases ran. (Per-class pass-rates
   and `total_passed/total_tests` ARE saved — only the aggregate `health_score` is None.)
2. **Target endpoint auth**: if the chatbot-under-test returns 401 (bad/missing bearer
   token), functional/quality/load score 0.0 (no valid responses). External — supply the
   target's token in the run config.
3. **Gemini `.env` key was spend-capped** (intermittent 429 "monthly spending cap"). The
   **NIA-provided** org key worked. For local fallback, use a key with quota.
4. **`ragas` import is broken** (`pyarrow 24` removed `PyExtensionType`, installed
   `datasets 2.14` still uses it). NOT imported by the pipeline (golden datasets use cached
   JSON), so it only affects the `/api/tools/status` panel. Fix if needed:
   `pip install -U datasets`.
5. **`/api/tools/status` 5s timeout** can show heavy tools (detoxify→torch) as "not
   loaded" on first import. Cosmetic.

---

## 11. Production deployment checklist

- [ ] `METRON_AUTH_BYPASS=0` (or remove); remove `METRON_DEV_ORG_ID`.
- [ ] `KEYCLOAK_ISSUER` + `KEYCLOAK_AUDIENCE=account` set to the prod realm.
- [ ] Confirm the platform proxy **forwards** the `Authorization` header to Metron.
- [ ] NIA: `NIA_DB_*` + `METRON_APPLICATION_NAME` (the app name the platform registered
      for Metron) + AWS KMS creds. Comment out `LLM_MODEL`/`LLM_API_KEY`.
- [ ] App DB `DB_*` → prod Postgres (SSL required in code).
- [ ] `CORS_ORIGINS` → the deployed frontend origin(s).
- [ ] `.env` is gitignored; never commit secrets. Delete/gitignore `test_litellm_hello.py`,
      `test_auth.py`, `_tok.txt`.
- [ ] (Recommended) fix issue #1 (`health_score`).
- [ ] Existing rows from the old SQLite `metron_runs.db` were NOT migrated (prod started
      fresh) — write a one-off copy script if that history is needed.

---

## 12. Platform integration reference (from the platform team)

- Keycloak issuer: `https://iam.dev.aifirstenterprise.ai/realms/yashtech_ai`
- `NEUPAC_KEYCLOAK_CLIENT_ID = unified_a2a_app` (not required by us — we don't role-gate).
- Bearer token marker: `$YashUnified2025$` (stripped before verify).
- NIA DB schema (read): `application_llm_config` (org_id, application_name, provider_id,
  model_id, provider_config_id, is_active) joined to `providers_llm_config` (config =
  KMS-encrypted JSON), `llm_models` (model_code), `llm_providers` (name).
- The platform's own reference files we ported from: `dynamic_config.py`, `llm.py`
  (uses `ChatLiteLLM`; we feed the same config into our existing `LLMClient` instead),
  the Keycloak middleware, and `test_litellm_hello.py` (runnable NIA test).
```

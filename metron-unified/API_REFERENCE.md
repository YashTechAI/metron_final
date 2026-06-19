# Metron API Reference

Backend: FastAPI (`fastapi_server.py`). Base path: all routes are under `/api`.
Interactive schema (request shapes + try-it-out): **`GET /docs`** (Swagger) and
**`GET /openapi.json`** when the server is running.

## Conventions

- **Auth.** Every endpoint except `GET /api/health` requires a valid Keycloak
  bearer token. Metron runs **embedded behind the platform's reverse proxy**, which
  injects `Authorization: Bearer <token>` on every request. The browser frontend
  sends no token itself — it just forwards cookies (`fetch(..., {credentials: "include"})`).
  A direct API tester must send the `Authorization` header manually (or run the
  backend with `METRON_AUTH_BYPASS=1` for local testing).
- **Content type.** JSON in / JSON out, *except* `POST /api/run` and
  `POST /api/parse-architecture`, which are `multipart/form-data` (file uploads).
- **Ownership.** Runs and projects are scoped to the caller's email; accessing
  another user's resource returns `403`.
- **Errors.** Standard FastAPI shape: `{"detail": "<message>"}` with a `4xx`/`5xx`
  status. Validation failures (e.g. missing required field) return `422`.

---

## Identity & status

### `GET /api/health`
No auth. Liveness probe.
```json
{ "status": "ok", "version": "2.0.0" }
```

### `GET /api/auth/me`
The caller's identity (this is the **single** identity endpoint).
```json
{ "email": "user@org.com", "role": "platform_admin", "organization_id": "<uuid>" }
```

### `GET /api/providers`
The server-configured evaluation LLM (the frontend shows this read-only — clients
never send LLM keys).
```json
{ "model": "gemini/gemini-2.5-flash", "configured": true }
```

### `GET /api/tools/status`
Availability of optional eval tools. Each key → `{installed, description, used_for}`.
Keys: `presidio`, `detoxify`, `llm_guard`, `deepeval`, `ragas`.

---

## Target-endpoint setup

### `POST /api/connect-test`
Smoke-test connectivity to the chatbot-under-test. Request (`ConnectTestRequest`):

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `endpoint_url` | string | — *(required)* | target chat endpoint |
| `request_field` | string | `"message"` | JSON key the target expects the message under |
| `response_field` | string | `"response"` | JSON key to read the reply from |
| `auth_type` | string | `"none"` | `"none"` or `"bearer"` |
| `auth_token` | string | `""` | bearer token for the target |
| `request_template` | string\|null | `null` | full JSON body with `{{query}}` / `{{uuid}}` / `{{conversation_id}}` placeholders |
| `response_trim_marker` | string\|null | `null` | drop everything at/after this marker in the reply |

Response: `{ "success": true, "message": "..." }` (never 500s — failures come back as `success:false`).

### `POST /api/parse-architecture`  *(multipart/form-data)*
Extract structured architecture fields from a document or diagram. Send **either**:
- `content` (form text field) — raw text from a `.txt`/`.pdf`/`.md`, **or**
- `image` (file) — PNG/JPG/WEBP of an architecture diagram.

Response: a JSON object of extracted architecture fields (used to pre-fill the
configure form). 400 if neither `content` nor `image` is provided.

---

## Persona/scenario preview

### `POST /api/preview`
Generate sample personas + scenarios for the configure page. Request (`PreviewRequest`):

| Field | Type | Default |
|-------|------|---------|
| `agent_description` | string | — *(required)* |
| `agent_domain` | string | `"general"` |
| `application_type` | string | `"chatbot"` |
| `num_personas` | int | `3` |
| `num_scenarios` | int | `5` |
| `organization_id` | string | server-set — **do not send** |

Response:
```json
{
  "personas": [
    { "id": "...", "name": "...", "description": "...", "traits": ["..."],
      "sample_prompts": ["..."], "fishbone": {}, "expertise": "...",
      "emotional_state": "...", "intent": "..." }
  ],
  "scenarios": [
    { "id": "...", "name": "...", "description": "...",
      "initial_prompt": "...", "expected_behavior": "...", "category": "functional" }
  ]
}
```

---

## Running a test

### `POST /api/run`  *(multipart/form-data)*
Starts the pipeline as a background job. Three parts:

| Part | Type | Required | Notes |
|------|------|----------|-------|
| `config` | **string** | yes | `JSON.stringify(runConfig)` — see the contract below |
| `document` | file | no | seed document (decoded to text) |
| `ground_truth_file` | file | no | CSV or JSON Q&A pairs (RAG); merged into `config.ground_truth` |

Backend does `JSON.parse` then validates against `RunConfig`. **Pydantic v2 ignores
unknown keys**, and **`endpoint_url` is the only required field** — everything else
has a default. Response:
```json
{ "run_id": "<uuid>", "project_id": "<uuid>" }
```

#### `config` field contract
| Group | Fields (default) |
|-------|------------------|
| **Target** | `endpoint_url` *(required)*, `request_field`=`"message"`, `response_field`=`"response"`, `auth_type`=`"none"`\|`"bearer"`, `auth_token`=`""`, `request_template`=`null`, `response_trim_marker`=`null`, `adapter_timeout`=`60`, `session_mode`=`"session_id"`\|`"history_injection"`\|`"messages_array"`\|`"none"` |
| **Project / agent** | `project_id`=`null`, `agent_name`=`""`, `agent_description`=`""`, `agent_domain`=`"general"`, `application_type`=`"chatbot"` |
| **RAG** | `is_rag`=`false`, `rag_text`=`""`, `ground_truth`=`[]` (each `{question, expected_answer}`) |
| **Test params** | `num_personas`=`3`, `num_scenarios`=`5`, `conversation_turns`=`3`, `enable_judge`=`true`, `performance_requests`=`20`, `load_concurrent_users`=`5`, `load_duration_seconds`=`30` |
| **Security** | `selected_attacks`=`["jailbreak","prompt_injection","pii_extraction","toxicity","encoding"]`, `attacks_per_category`=`3`, `garak_mode`=`"basic"`\|`"off"`\|`"full"` |
| **Quality** | `ragas_metrics`=`["faithfulness","answer_relevancy"]`, `deepeval_metrics`=`["hallucination","answer_relevancy"]`, `use_geval`=`true` |
| **Notify** | `notify_email`=`false` |
| **Architecture / RCA** *(all optional, free-form)* | `deployment_type`, `vector_db`, `session_db`, `cache_layer`, `message_queue`, `api_gateway`, `auth_mechanism`, `monitoring_tool`, `is_multi_region`, `has_rate_limiting`, `rate_limit_rpm`, `has_retry_logic`, `has_circuit_breaker`, `has_caching`, `has_dlq`, `additional_architecture_notes`, `architecture_document` |
| **Server-set — do NOT send** | `organization_id` (overwritten from the JWT) |

#### Example (browser)
```js
const fd = new FormData();
fd.append("config", JSON.stringify({
  endpoint_url: "https://my-ai.example.com/api/chat",
  agent_domain: "banking",
  application_type: "chatbot",
  num_personas: 3,
}));
// fd.append("document", file);            // optional
// fd.append("ground_truth_file", csv);    // optional (RAG)
const res = await fetch("/api/run", { method: "POST", body: fd, credentials: "include" });
const { run_id } = await res.json();
```

### `GET /api/job/{run_id}/status`
Poll while running. (Recovers from DB if the server restarted.)
```json
{
  "run_id": "...", "status": "queued|running|completed|failed",
  "progress": 0-100, "message": "...", "current_phase": "...",
  "phase_results": {}, "log_events": [], "eval_warnings": [], "error": null
}
```

### `GET /api/job/{run_id}/results`
The full report JSON once complete. Top-level keys include `health_score`,
`passed`, `total_tests`, `total_passed`, the per-phase blocks (`functional`,
`security`, `quality`, `rag`, `performance`, `load`), `personas`,
`persona_breakdown`, `rca`, `quality_criteria`, `user_role`.

> ⚠️ Status-code quirk: while still running this returns **`202`** (as an error
> body), and a failed pipeline returns **`500`** with the error message. Treat both
> as non-fatal states, not server crashes — prefer polling `/status` first.

### `GET /api/job/{run_id}/token-summary`
LLMOps usage for a run. Populated runs return the full summary
(`total_calls`, `total_tokens`, `estimated_cost_usd`, `by_stage`, …); otherwise
`{ "total_calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0 }`.

---

## Projects & history

### `POST /api/projects`
Create/update a project. Request (`_ProjectBody`):
`{ project_id, name, endpoint, api_key?="", document_text?="", document_name?="" }`
→ `{ "ok": true }`

### `GET /api/projects`
`{ "projects": [ { ...project } ] }` — only the caller's projects.

### `GET /api/projects/{project_id}`
A single project object.
> ⚠️ Known issue: this endpoint currently does **not** verify ownership, so any
> authenticated user can read any project (including its stored `api_key`). Treat
> the stored `api_key` as sensitive until this is fixed.

### `DELETE /api/projects/{project_id}`
Owner-only. `{ "ok": true }` (403 if not yours, 404 if missing).

### `GET /api/project/{project_id}/runs`
Run history for a project (owner-only): DB rows + any in-memory runs not yet persisted.
```json
{ "project_id": "...", "runs": [
  { "run_id": "...", "project_id": "...", "timestamp": "...",
    "health_score": 0.0, "domain": "...", "application_type": "...", "status": "..." }
] }
```

---

## Removed in this cleanup (do not reference)
`GET /api/quota` (use `GET /api/auth/me`), `POST /api/parse-document` (use
`POST /api/parse-architecture`), `GET /api/runs/{a}/compare/{b}`,
`POST /api/auth/logout` (logout is the host platform's responsibility). The dead
LLM-credential fields (`llm_provider`, `llm_api_key`, `azure_endpoint`, `aws_*`,
`bedrock_model_id`) were removed from request models — the evaluation LLM is
resolved server-side per organization; clients must never send LLM keys.

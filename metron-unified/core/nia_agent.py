"""
NIA A2A agent target config — the fixed, server-side adapter configuration used
when METRON runs embedded inside the NIA platform to test NIA's own A2A agents.

The UI exposes only the endpoint URL (auto-selected) + Test Connection; every other
target-adapter setting (request template, response path, trim marker, request field,
auth) is fixed here rather than typed by the user.

The A2A protocol requires the caller's Keycloak JWT inside the request body at
`params.message.parts[].token`. That JWT arrives on the request's Authorization
header (the NIA gateway injects it, same as the token core.auth already verifies)
and is forwarded into the body at send-time via `RunConfig.injected_token` → the
`{{token}}` placeholder (see ChatbotAdapter._build_payload). It is NOT baked into
the stored template, persisted, or logged.

Config (env vars):
  METRON_NIA_TEAM_ID   the agent-under-test's team_id (the agent uses it to pick
                       its own LLM). Defaults to the current agent under test.
"""
from __future__ import annotations
import os

# A2A JSON-RPC "tasks/send" envelope. Send-time placeholders (resolved by the
# adapter): {{query}} {{uuid}} {{conversation_id}} {{token}}. {{team_id}} is baked
# in once by nia_request_template() below — it is static per agent, not per request.
NIA_A2A_REQUEST_TEMPLATE = """{
  "id": "{{uuid}}",
  "task_id": "{{uuid}}",
  "method": "tasks/send",
  "params": {
    "id": "{{uuid}}",
    "task_id": "{{uuid}}",
    "message": {
      "kind": "message",
      "messageId": "{{uuid}}",
      "parts": [
        { "type": "text", "kind": "text", "text": "{{query}}" },
        { "type": "auth", "token": "{{token}}" }
      ],
      "role": "user",
      "metadata": {}
    },
    "metadata": {
      "user_metadata": {
        "team_id": "{{team_id}}",
        "session_id": "{{conversation_id}}"
      }
    }
  }
}"""

# Where the answer text lives in a successful A2A response. Dot-notation with list
# indices — handled by ChatbotAdapter._extract (and its _try_extract_a2a fallback).
NIA_A2A_RESPONSE_FIELD = "result.artifacts.0.parts.0.text"

# The agent appends follow-up suggestions after this marker; strip them before eval.
NIA_A2A_RESPONSE_TRIM_MARKER = "FOLLOW UP QUESTIONS"

# Ignored while a request_template is set, but kept consistent with the template.
NIA_A2A_REQUEST_FIELD = "message"

# Default team_id = the current agent under test. Override per deployment via env.
_DEFAULT_TEAM_ID = "9239662f-57ff-4a9c-8c9d-fdf06ff0121a"


def _team_id() -> str:
    return os.environ.get("METRON_NIA_TEAM_ID", "").strip() or _DEFAULT_TEAM_ID


def nia_request_template() -> str:
    """The A2A request template with team_id baked in ({{token}} still a placeholder)."""
    return NIA_A2A_REQUEST_TEMPLATE.replace("{{team_id}}", _team_id())


def apply_nia_agent_config(run_config, token: str) -> None:
    """Overwrite a RunConfig's target-adapter settings with the fixed NIA A2A config.

    Mirrors how /api/run server-sets organization_id from the JWT: whatever the
    client sent for these fields is ignored. The caller's JWT is attached via
    injected_token (a non-persisted field) and injected into {{token}} at send-time.
    endpoint_url is left as-is (it comes from the UI dropdown).
    """
    run_config.request_template     = nia_request_template()
    run_config.response_field       = NIA_A2A_RESPONSE_FIELD
    run_config.response_trim_marker = NIA_A2A_RESPONSE_TRIM_MARKER
    run_config.request_field        = NIA_A2A_REQUEST_FIELD
    # application_type is intentionally NOT overridden: the user still picks the
    # agent type (chatbot / rag / multi_agent / form), which drives test generation
    # and evaluation. Only the A2A request envelope + token injection are fixed here.
    # A2A auth lives in the body token, not an HTTP header — make sure no stray
    # UI-entered bearer token is sent as an Authorization header to the agent.
    run_config.auth_type            = "none"
    run_config.auth_token           = ""
    run_config.injected_token       = token or ""

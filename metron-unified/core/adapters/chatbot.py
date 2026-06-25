"""
ChatbotAdapter — generic JSON REST chatbot.
Supports dot-notation response field extraction (e.g. "output.text.value").
Supports full JSON request templates with {{query}}, {{uuid}}, {{conversation_id}} placeholders.
"""

from __future__ import annotations
import json
import time
import uuid as _uuid_mod
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import aiohttp


@dataclass
class AdapterResponse:
    text:              str
    latency_ms:        float
    retrieved_context: Optional[List[str]] = None
    agent_trace:       Optional[List[Dict]] = None
    error:             Optional[str] = None
    raw_data:          Optional[Dict] = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.text.startswith("[Error")


class _SessionMixin:
    """Shared aiohttp session management for adapters.

    Default (persistent_session=False): a fresh ClientSession per send() — the original
    behavior, used by one-shot callers (connect-test, performance evaluation).

    persistent_session=True: ONE ClientSession (with its cookie jar) is reused across all
    send() calls, so cookie-based conversation continuity survives multiple turns. The
    adapter is created per-conversation and its turns run sequentially, so no locking is
    needed. The owner (run_conversation) must call aclose() when the conversation ends.
    """

    def _init_session(self, persistent: bool) -> None:
        self._persistent_session = persistent
        self._session: Optional[aiohttp.ClientSession] = None

    async def _acquire_session(self) -> Tuple[aiohttp.ClientSession, bool]:
        """Return (session, should_close_after_use)."""
        if not self._persistent_session:
            return aiohttp.ClientSession(), True
        if self._session is None or self._session.closed:
            # unsafe=True so cookies are stored even for bare IP-address endpoints
            # (test targets are often http://<ip> / localhost, which a safe jar drops).
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        return self._session, False

    async def aclose(self) -> None:
        """Close the persistent session, if any. Safe on non-persistent adapters."""
        session = getattr(self, "_session", None)
        if session is not None and not session.closed:
            await session.close()
        self._session = None


class ChatbotAdapter(_SessionMixin):
    """
    Sends {request_field: message} → reads {response_field: answer}.
    If request_template is set, renders the full JSON body instead.
    Supports nested response fields with dot notation.
    """

    def __init__(
        self,
        endpoint_url:         str,
        request_field:        str = "message",
        response_field:       str = "response",
        auth_type:            str = "none",
        auth_token:           str = "",
        timeout:              int = 30,
        request_template:     Optional[str] = None,
        response_trim_marker: Optional[str] = None,
        session_mode:         str = "session_id",
        persistent_session:   bool = False,
    ):
        self.endpoint_url         = endpoint_url
        self.request_field        = request_field
        self.response_field       = response_field
        self.timeout              = timeout
        self.request_template     = request_template
        self.response_trim_marker = response_trim_marker
        self.session_mode         = session_mode
        self._init_session(persistent_session)
        self.headers: Dict[str, str] = {"Content-Type": "application/json"}
        if auth_token and "bearer" in auth_type.lower():
            self.headers["Authorization"] = f"Bearer {auth_token}"

    def _build_payload(
        self,
        message: str,
        conversation_id: str,
        history: Optional[List[Dict]] = None,
    ) -> dict:
        history = history or []
        mode = self.session_mode

        # messages_array: build OpenAI-style list; template is irrelevant in this mode.
        if mode == "messages_array":
            messages = list(history)   # [{"role": "user"/"assistant", "content": "..."}]
            messages.append({"role": "user", "content": message})
            return {self.request_field: messages}

        if self.request_template:
            # json.dumps escapes newlines, tabs, quotes etc; strip outer quotes to get bare escaped string
            escaped = json.dumps(message)[1:-1]
            body_str = (
                self.request_template
                .replace("{{query}}", escaped)
                .replace("{{uuid}}", str(_uuid_mod.uuid4()))
                .replace("{{conversation_id}}", conversation_id or str(_uuid_mod.uuid4()))
            )
            if mode == "history_injection" and "{{history}}" in body_str:
                # Escape the history string so it safely embeds inside the JSON template.
                history_escaped = json.dumps(self._build_history_string(history))[1:-1]
                body_str = body_str.replace("{{history}}", history_escaped)
            return json.loads(body_str)

        # No template — plain single-field payload.
        if mode == "history_injection" and history:
            history_str = self._build_history_string(history)
            return {self.request_field: f"{history_str}\nUser: {message}"}
        return {self.request_field: message}

    @staticmethod
    def _build_history_string(history: List[Dict]) -> str:
        lines = []
        for turn in history:
            role = "User" if turn.get("role") == "user" else "Assistant"
            lines.append(f"{role}: {turn.get('content', '')}")
        return "\n".join(lines)

    def _trim_response(self, text: str) -> str:
        if self.response_trim_marker and self.response_trim_marker in text:
            return text[:text.index(self.response_trim_marker)].rstrip()
        return text

    async def send(
        self,
        message: str,
        history: Optional[List] = None,
        conversation_id: str = "",
    ) -> AdapterResponse:
        start = time.monotonic()
        session, _close_session = await self._acquire_session()
        try:
            payload = self._build_payload(message, conversation_id, history)
            async with session.post(
                self.endpoint_url,
                json=payload,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                latency = (time.monotonic() - start) * 1000
                if resp.status != 200:
                    return AdapterResponse("", latency, error=f"HTTP {resp.status}")
                data = await resp.json(content_type=None)
                text = self._trim_response(self._extract(data, self.response_field))
                # If configured path fails, try A2A protocol auto-detection before giving up.
                if text.startswith(("[Field ", "[Index ", "[Empty", "[No item")):
                    a2a_text = self._try_extract_a2a(data)
                    if a2a_text:
                        text = self._trim_response(a2a_text)
                    else:
                        return AdapterResponse("", latency, error=text, raw_data=data)
                return AdapterResponse(text, latency, raw_data=data)
        except aiohttp.ClientConnectorError as e:
            latency = (time.monotonic() - start) * 1000
            return AdapterResponse("", latency, error=f"Connection refused: {e}")
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return AdapterResponse("", latency, error=str(e))
        finally:
            if _close_session:
                await session.close()

    async def test_connection(self) -> Tuple[bool, str]:
        resp = await self.send("Hello, this is a connectivity test.")
        if resp.ok:
            return True, f"Connected. Latency: {resp.latency_ms:.0f}ms"
        # A2A protocol: endpoint returns {"result": {"status": {"state": "..."}}} even
        # when artifacts aren't present (e.g. state="input-required" for a greeting).
        # The endpoint is live — extraction failing on the test message is expected.
        if resp.raw_data and "result" in resp.raw_data:
            state = (resp.raw_data["result"].get("status") or {}).get("state", "responded")
            return True, f"Connected (A2A, state={state}). Latency: {resp.latency_ms:.0f}ms"
        return False, resp.error or "Unknown error"

    @staticmethod
    def _extract(data: dict, field_path: str) -> str:
        parts = field_path.split(".")
        result: object = data
        for part in parts:
            if isinstance(result, dict) and part in result:
                result = result[part]
            elif isinstance(result, list) and part.isdigit():
                idx = int(part)
                result = result[idx] if 0 <= idx < len(result) else "[Index OOB]"
            elif isinstance(result, list) and "[" in part and "=" in part:
                # Array filter syntax: field[key=value] — selects first matching item.
                # e.g. "parts[type=text]" picks the first part where type == "text".
                filter_expr = part.split("[", 1)[1].rstrip("]")
                filter_key, filter_val = filter_expr.split("=", 1)
                matched = next(
                    (item for item in result if isinstance(item, dict) and item.get(filter_key) == filter_val),
                    None,
                )
                if matched is not None:
                    result = matched
                else:
                    return f"[No item with {filter_key}={filter_val} in array]"
            else:
                return f"[Field '{part}' not found]"
        return str(result) if result is not None else "[Empty response]"

    @staticmethod
    def _try_extract_a2a(data: dict) -> str:
        """Auto-extract text from an A2A protocol response.

        Walks result.artifacts[].parts[] and returns the first part
        where type == 'text', regardless of position in the array.
        This handles mixed-type parts arrays (text + auth, etc.).
        """
        try:
            artifacts = (data.get("result") or {}).get("artifacts") or []
            for artifact in artifacts:
                for part in artifact.get("parts") or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if text:
                            return text
        except Exception:
            pass
        return ""

"""
Keycloak JWT auth. Verifies RS256 Bearer tokens issued by the host platform's
Keycloak realm. Metron runs embedded behind a reverse proxy that injects the
`Authorization: Bearer <token>` header on every request.

Authorization is intentionally NOT enforced here: any request carrying a valid
Keycloak token is granted full access to the platform. User/role/tenant gating
has been removed — the host platform decides who may reach Metron at all.

JWKS keys are cached for 1 hour to avoid repeated network fetches.

Config (env vars):
  KEYCLOAK_URL       Base URL of the Keycloak server, e.g. https://auth.example.com
  KEYCLOAK_REALM     Realm name, e.g. metron
  KEYCLOAK_AUDIENCE  (optional) Expected `aud`. If unset, audience is not checked
                     (Keycloak access tokens often carry aud="account").
  KEYCLOAK_ISSUER    (optional) Override the derived issuer URL.
  KEYCLOAK_JWKS_URL  (optional) Override the derived JWKS URL.
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
from typing import Optional

from jose import jwt, jwk
from fastapi import HTTPException, Request

# Every authenticated user is granted this role — it unlocks all pipeline phases
# and passes every admin/super check, i.e. full access to the platform.
_FULL_ACCESS_ROLE = "platform_admin"


def _base_url() -> str:
    return os.environ.get("KEYCLOAK_URL", "").rstrip("/")


def _realm() -> str:
    return os.environ.get("KEYCLOAK_REALM", "")


def _issuer() -> str:
    override = os.environ.get("KEYCLOAK_ISSUER", "").strip()
    if override:
        return override.rstrip("/")
    base, realm = _base_url(), _realm()
    if not base or not realm:
        raise RuntimeError("KEYCLOAK_URL and KEYCLOAK_REALM env vars must be set")
    return f"{base}/realms/{realm}"


def _jwks_url() -> str:
    override = os.environ.get("KEYCLOAK_JWKS_URL", "").strip()
    if override:
        return override
    return f"{_issuer()}/protocol/openid-connect/certs"


_jwks_cache: dict = {"keys": {}, "expires": 0.0}


def _fetch_jwks() -> dict:
    now = time.monotonic()
    if _jwks_cache["expires"] > now and _jwks_cache["keys"]:
        return _jwks_cache["keys"]
    url = _jwks_url()
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    keys = {k["kid"]: k for k in data["keys"]}
    _jwks_cache.update({"keys": keys, "expires": now + 3600})
    return keys


def verify_token(token: str) -> Optional[dict]:
    """Verify a Keycloak RS256 access/ID token. Returns {"email": ...} or None."""
    try:
        headers = jwt.get_unverified_headers(token)
        kid = headers.get("kid")
        keys = _fetch_jwks()
        if kid not in keys:
            _jwks_cache["expires"] = 0.0  # force refresh once (handles key rotation)
            keys = _fetch_jwks()
        if kid not in keys:
            return None
        public_key = jwk.construct(keys[kid])

        audience = os.environ.get("KEYCLOAK_AUDIENCE", "").strip()
        decode_opts = {"verify_aud": bool(audience)}
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=audience or None,
            issuer=_issuer(),
            options=decode_opts,
        )
        # Keycloak: prefer email; fall back to preferred_username, then subject.
        email = (
            claims.get("email")
            or claims.get("preferred_username")
            or claims.get("sub", "")
        )
        return {"email": email}
    except Exception as exc:
        print(f"[Auth] Token verification failed: {exc}")
        return None


def get_current_user(request: Request) -> dict:
    """
    Authenticate the caller via the injected Keycloak Bearer token.

    No authorization is performed: every valid token yields a full-access user.
    There is no per-user/tenant/role gating and no access-denied path.

    Set METRON_AUTH_BYPASS=1 in .env to skip token verification during local
    development (never set this in production).
    """
    if os.environ.get("METRON_AUTH_BYPASS", "").strip() == "1":
        return {"email": "dev@local.test", "role": _FULL_ACCESS_ROLE, "tenant_id": ""}

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth_header[7:]
    user = verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Full permission for everyone — no DB lookup, no role/tenant restriction.
    user["role"] = _FULL_ACCESS_ROLE
    user["tenant_id"] = ""
    return user

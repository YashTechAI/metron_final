"""
Seed-document profile cache.

Extracts AppProfile + TechnicalProfile from a project's seed document (via the LLM) and
caches the result in the `projects` table, keyed by a SHA-256 of the document text.
Re-extracts only when the seed document changes. Used by `/api/preview` and pipeline
stage 0 so the *same* uniform profile drives persona + functional/security/quality
prompt generation — without re-running the two extraction passes on every run.
"""

from __future__ import annotations
import hashlib
from typing import Optional, Tuple

from core import db as _db
from core.llm_client import LLMClient
from core.models import AppProfile, TechnicalProfile
from stages.s0_profile.document_parser import parse_document
from stages.s0_profile.technical_extractor import extract_technical_profile


def doc_sha(text: str) -> str:
    """Stable hash of the seed-document text — the cache key."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


async def get_or_build_profiles(
    doc_text: str,
    llm_client: LLMClient,
    project_id: str = "",
    project: Optional[dict] = None,
) -> Tuple[AppProfile, TechnicalProfile]:
    """Return (AppProfile, TechnicalProfile) for the seed doc, using the per-project cache.

    - Cache hit: a profile exists for this project AND the seed-doc SHA matches →
      deserialize from JSON (no LLM call).
    - Cache miss: run parse_document + extract_technical_profile, persist to the project
      (when project_id is known and the doc is non-empty), and return.

    `project` (optional) is a pre-loaded `projects` row to avoid a redundant DB read; if
    omitted and `project_id` is set, it is loaded here.
    """
    doc_text = doc_text or ""
    if project is None and project_id:
        try:
            project = _db.get_project(project_id)
        except Exception as e:
            print(f"[ProfileCache] get_project failed ({e}); extracting without cache")
            project = None

    sha = doc_sha(doc_text)

    # ── Cache hit ────────────────────────────────────────────────────────────
    if project and project.get("document_sha") == sha and project.get("profile_json"):
        try:
            profile = AppProfile.model_validate_json(project["profile_json"])
            tech_raw = project.get("tech_profile_json")
            tech = (TechnicalProfile.model_validate_json(tech_raw)
                    if tech_raw else TechnicalProfile())
            return profile, tech
        except Exception as e:
            print(f"[ProfileCache] cached profile deserialize failed ({e}); re-extracting")

    # ── Cache miss → extract (two LLM passes) ────────────────────────────────
    profile = await parse_document(doc_text, llm_client, project_id)
    tech = await extract_technical_profile(doc_text, llm_client)

    # ── Persist (best-effort; never block the run on a cache write) ──────────
    if project_id and doc_text.strip():
        try:
            _db.save_project_profile(
                project_id,
                profile.model_dump_json(),
                tech.model_dump_json(),
                sha,
            )
        except Exception as e:
            print(f"[ProfileCache] save failed (non-fatal): {e}")

    return profile, tech

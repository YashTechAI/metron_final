"""
Per-organization LLM config resolution — ported from the platform's deployed
reference. Reads the org's active LLM config from the central NIA database and
KMS-decrypts the provider config (which holds the API key).

Config (env vars):
  NIA_DATABASE_URL / NIA_DB_*   NIA database connection (see core.nia_connection)
  METRON_APPLICATION_NAME       this app's name in application_llm_config
  AWS_REGION, AWS_ACCESS_KEY, AWS_SECRET_KEY   KMS decrypt credentials

Cached 60s (TTLCache) to allow fast propagation of config changes from NIA DB.
"""
from __future__ import annotations
import base64
import json
import os
from typing import Any, Dict

import boto3
from cachetools import TTLCache, cached
from sqlalchemy import text

from core.nia_connection import nia_engine

# Cache for 1 minute (matches the reference) to propagate NIA config changes fast.
config_cache: TTLCache = TTLCache(maxsize=100, ttl=60)


def _application_name(application_name: str = "") -> str:
    return application_name or os.environ.get("METRON_APPLICATION_NAME", "RAI_SERVICE")


@cached(cache=config_cache)
def fetch_llm_config_and_pricing(organization_id: str, application_name: str) -> Dict[str, Any]:
    """
    Fetch an organization's active LLM configuration from the NIA database and
    KMS-decrypt the provider config.

    Returns: {provider_name, model_code, decrypted_config}.
    Raises ValueError if NIA DB is unconfigured, no active config exists, or
    decryption fails.
    """
    engine = nia_engine()
    if engine is None:
        raise ValueError("NIA database is not configured (set NIA_DB_* / NIA_DATABASE_URL)")

    print(f"[DynamicConfig] Fetching LLM config for org '{organization_id}', "
          f"app '{application_name}' from NIA DB")

    sql_query = text("""
        SELECT
            lp.name AS provider_name,
            lm.model_code,
            plc.config AS encrypted_config
        FROM application_llm_config alc
        JOIN providers_llm_config plc ON plc.id = alc.provider_config_id
        JOIN llm_models lm ON lm.id = alc.model_id
        JOIN llm_providers lp ON lp.id = alc.provider_id
        WHERE alc.organization_id = CAST(:org_id AS UUID)
          AND alc.application_name = :app_name
          AND alc.is_active = TRUE
          AND plc.is_active = TRUE
        LIMIT 1
    """)

    with engine.connect() as conn:
        result = conn.execute(
            sql_query, {"org_id": organization_id, "app_name": application_name}
        ).first()

        if not result:
            raise ValueError(
                f"No active ApplicationLLMConfig found for org '{organization_id}' "
                f"and app '{application_name}'"
            )

        provider_name = result.provider_name
        model_code = result.model_code
        encrypted_config_b64 = result.encrypted_config

    # Decrypt KMS config
    try:
        kms_client = boto3.client(
            "kms",
            region_name=(os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "")),
            aws_access_key_id=(os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY", "")),
            aws_secret_access_key=(os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_KEY", "")),
        )
        encrypted_bytes = base64.b64decode(encrypted_config_b64)
        kms_response = kms_client.decrypt(CiphertextBlob=encrypted_bytes)
        decrypted_text = kms_response["Plaintext"].decode("utf-8")
        decrypted_config = json.loads(decrypted_text)
    except Exception as e:
        print(f"[DynamicConfig] Failed to decrypt KMS config: {e}")
        raise ValueError("Failed to decrypt LLM configuration") from e

    return {
        "provider_name": provider_name,
        "model_code": model_code,
        "decrypted_config": decrypted_config,
    }

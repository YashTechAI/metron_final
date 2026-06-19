"""
Per-organization LLM config resolution — ported from the platform's deployed
reference (dynamic_config.py). Reads the org's active LLM config + pricing from
the central NIA database and KMS-decrypts the provider config (which holds the
API key).

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
    Fetch the LLM configuration and pricing for an organization from the NIA
    database, decrypting the KMS-encrypted provider config.

    Returns: {provider_name, model_code, decrypted_config, pricing_info}.
    Raises ValueError if NIA DB is unconfigured, no active config exists, or
    decryption fails.
    """
    engine = nia_engine()
    if engine is None:
        raise ValueError("NIA database is not configured (set NIA_DATABASE_URL)")

    print(f"[DynamicConfig] Fetching LLM config for org '{organization_id}', "
          f"app '{application_name}' from NIA DB")

    sql_query = text("""
        SELECT
            lp.name AS provider_name,
            lm.model_code,
            plc.config AS encrypted_config,
            lmp.price_per_million_input_selling,
            lmp.price_per_million_output_selling,
            lmp.currency
        FROM application_llm_config alc
        JOIN providers_llm_config plc ON plc.id = alc.provider_config_id
        JOIN llm_models lm ON lm.id = alc.model_id
        JOIN llm_providers lp ON lp.id = alc.provider_id

        /*
          LEFT JOIN allows fetching the config even if pricing isn't defined yet.
          The IN clause acts as a fallback: if the tenant doesn't have custom
          pricing, it falls back to the master/provider organization's base pricing.
        */
        LEFT JOIN llm_model_pricing lmp
            ON lmp.model_id = alc.model_id
            AND lmp.organization_id IN (alc.organization_id, lp.organization_id)
            AND CURRENT_TIMESTAMP >= lmp.effective_from
            AND (lmp.effective_to IS NULL OR CURRENT_TIMESTAMP < lmp.effective_to)

        /* Explicitly cast the parameter to UUID to prevent Postgres type mismatch errors */
        WHERE alc.organization_id = CAST(:org_id AS UUID)
          AND alc.application_name = :app_name
          AND alc.is_active = TRUE
          AND plc.is_active = TRUE
          AND lm.is_deleted = FALSE

        /*
          Prioritize tenant-specific pricing over the fallback master pricing,
          then get the most recent effective pricing. NULLS LAST prevents empty
          pricing rows from bubbling to the top.
        */
        ORDER BY
            (lmp.organization_id = alc.organization_id) DESC,
            lmp.effective_from DESC NULLS LAST
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

        pricing_info: Dict[str, Any] = {}
        if (result.price_per_million_input_selling is not None
                and result.price_per_million_output_selling is not None):
            pricing_info = {
                "price_per_million_input_selling": float(result.price_per_million_input_selling),
                "price_per_million_output_selling": float(result.price_per_million_output_selling),
                "currency": result.currency,
            }
        else:
            print(f"[DynamicConfig] No pricing found for model '{model_code}' "
                  f"in org '{organization_id}'")

    # Decrypt KMS config
    try:
        kms_client = boto3.client(
            "kms",
            region_name=os.environ.get("AWS_REGION", ""),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY", ""),
            aws_secret_access_key=os.environ.get("AWS_SECRET_KEY", ""),
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
        "pricing_info": pricing_info,
    }

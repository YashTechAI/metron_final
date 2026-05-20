"""
Alerting module for Metron LLMOps.

Sends Slack webhook and/or SMTP email notifications when:
  - Health score drops below ALERT_HEALTH_THRESHOLD (default 0.70)
  - Regression is detected (health dropped > ALERT_REGRESSION_THRESHOLD vs previous run)

All functions are best-effort — exceptions are logged but never re-raised so that
alerting failures cannot crash the pipeline.

Required env vars (all optional — missing vars disable the corresponding channel):
  SLACK_WEBHOOK_URL          Slack incoming webhook URL
  SMTP_HOST                  e.g. smtp.gmail.com
  SMTP_PORT                  default 587
  SMTP_USER                  SMTP login username
  SMTP_PASSWORD              SMTP login password
  ALERT_FROM_EMAIL           Sender address
  ALERT_TO_EMAILS            Comma-separated recipient list
  ALERT_HEALTH_THRESHOLD     Float 0-1, default 0.70
  ALERT_REGRESSION_THRESHOLD Float 0-1, default 0.05 (also used by pipeline.py)
"""

from __future__ import annotations
import json
import os
import smtplib
import urllib.request
from email.mime.text import MIMEText
from typing import Optional


def send_slack(webhook_url: str, message: str) -> None:
    """POST a plain-text message to a Slack incoming webhook."""
    payload = json.dumps({"text": message}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Slack webhook returned HTTP {resp.status}")


def send_email(to: list[str], subject: str, body: str) -> None:
    """Send a plain-text email via SMTP (TLS on port 587 by default)."""
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("ALERT_FROM_EMAIL", user)

    if not host or not user or not password:
        raise ValueError("SMTP_HOST, SMTP_USER, and SMTP_PASSWORD must be set for email alerts")

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(from_addr, to, msg.as_string())


def fire_alerts(
    project_id: str,
    run_id: str,
    health_score: float,
    regression_detected: bool,
    health_delta: float,
) -> None:
    """
    Evaluate alert conditions and dispatch to configured channels.

    Called from pipeline.py after MLflow logging. Silently skips channels
    whose env vars are absent.
    """
    health_threshold = float(os.environ.get("ALERT_HEALTH_THRESHOLD", "0.70"))
    low_health = health_score < health_threshold

    if not low_health and not regression_detected:
        return  # nothing to alert on

    # Build human-readable message
    reasons: list[str] = []
    if low_health:
        reasons.append(
            f"Health score {health_score:.1%} is below the threshold of {health_threshold:.1%}"
        )
    if regression_detected:
        reasons.append(
            f"Regression detected: health dropped {abs(health_delta):.1%} vs previous run"
        )

    message = (
        f"*Metron Alert* — Project `{project_id}`\n"
        f"Run: `{run_id}`\n"
        f"Health Score: {health_score:.1%}\n"
        + "\n".join(f"• {r}" for r in reasons)
    )

    # Slack
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if slack_url:
        try:
            send_slack(slack_url, message)
            print(f"[Alerts] Slack notification sent for run {run_id}")
        except Exception as e:
            print(f"[Alerts] Slack send failed (non-fatal): {e}")

    # Email
    to_raw = os.environ.get("ALERT_TO_EMAILS", "")
    if to_raw:
        to_list = [addr.strip() for addr in to_raw.split(",") if addr.strip()]
        subject = f"[Metron] Alert: {', '.join(reasons[:1])}"
        try:
            send_email(to_list, subject, message.replace("*", "").replace("`", ""))
            print(f"[Alerts] Email notification sent for run {run_id}")
        except Exception as e:
            print(f"[Alerts] Email send failed (non-fatal): {e}")

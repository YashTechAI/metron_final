"""
Email notifications via AWS SES.

Sends two types of emails:
  1. Run completed — health score summary + HTML report as attachment
  2. Run failed    — error details so user knows what went wrong

AWS credentials are read from environment variables (already present in .env):
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
  SES_SENDER_EMAIL  — verified sender address in SES console
"""

from __future__ import annotations
import os
import email as _email_lib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime
from typing import Optional


# ── Read config from environment ──────────────────────────────────────────────
_SENDER       = os.environ.get("SES_SENDER_EMAIL", "")
_REGION       = os.environ.get("SES_REGION") or os.environ.get("AWS_REGION", "us-east-1")
_ACCESS_KEY   = os.environ.get("AWS_ACCESS_KEY_ID", "")
_SECRET_KEY   = os.environ.get("AWS_SECRET_ACCESS_KEY", "")


def _ses_client():
    """Return a boto3 SES client. Returns None if boto3 or credentials are missing."""
    try:
        import boto3
        return boto3.client(
            "ses",
            region_name=_REGION,
            aws_access_key_id=_ACCESS_KEY,
            aws_secret_access_key=_SECRET_KEY,
        )
    except ImportError:
        print("[Notifications] boto3 not installed — email notifications disabled.")
        return None
    except Exception as e:
        print(f"[Notifications] Failed to create SES client: {e}")
        return None


def _send_raw(to_email: str, subject: str, body_html: str, attachment_html: Optional[str] = None, attachment_filename: str = "report.html") -> bool:
    """
    Send an email via AWS SES.
    - body_html: the email body as HTML
    - attachment_html: optional HTML file to attach (the full report)
    Returns True if sent successfully, False otherwise.
    """
    if not _SENDER:
        print("[Notifications] SES_SENDER_EMAIL not set — skipping email.")
        return False
    if not to_email:
        print("[Notifications] No recipient email — skipping.")
        return False

    client = _ses_client()
    if not client:
        return False

    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = _SENDER
        msg["To"]      = to_email

        # Email body
        msg.attach(MIMEText(body_html, "html"))

        # HTML report as attachment
        if attachment_html:
            part = MIMEApplication(attachment_html.encode("utf-8"), Name=attachment_filename)
            part["Content-Disposition"] = f'attachment; filename="{attachment_filename}"'
            msg.attach(part)

        client.send_raw_email(
            Source=_SENDER,
            Destinations=[to_email],
            RawMessage={"Data": msg.as_string()},
        )
        print(f"[Notifications] Email sent to {to_email}: {subject}")
        return True

    except Exception as e:
        print(f"[Notifications] Failed to send email to {to_email}: {e}")
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def notify_run_complete(
    user_email:   str,
    agent_name:   str,
    domain:       str,
    health_score: Optional[float],
    passed:       bool,
    total_tests:  int,
    total_passed: int,
    run_id:       str,
    report_html:  Optional[str] = None,
    is_full_run:  bool = True,
) -> None:
    """Send a 'run completed' email with the HTML report attached."""

    score_line = (
        f"<strong>Health Score:</strong> {round(health_score * 100)}% — {'✅ PASSED' if passed else '❌ FAILED'}"
        if is_full_run and health_score is not None
        else f"<strong>Tests Passed:</strong> {total_passed} / {total_tests}"
    )

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #1a1a1a; max-width: 600px; margin: 0 auto;">

      <div style="background: linear-gradient(135deg, #00668a, #004f6b); padding: 32px; border-radius: 12px 12px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 24px;">MetronAI</h1>
        <p style="color: rgba(255,255,255,0.8); margin: 8px 0 0 0; font-size: 14px;">AI Quality Intelligence Platform</p>
      </div>

      <div style="background: #f9f9f9; padding: 32px; border-radius: 0 0 12px 12px; border: 1px solid #e0e0e0;">
        <h2 style="color: #1a1a1a; margin-top: 0;">Your test run is complete</h2>

        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
          <tr>
            <td style="padding: 10px 0; color: #666; font-size: 13px; width: 40%;">Agent</td>
            <td style="padding: 10px 0; font-weight: bold; font-size: 13px;">{agent_name or "—"}</td>
          </tr>
          <tr style="border-top: 1px solid #eee;">
            <td style="padding: 10px 0; color: #666; font-size: 13px;">Domain</td>
            <td style="padding: 10px 0; font-size: 13px;">{domain or "—"}</td>
          </tr>
          <tr style="border-top: 1px solid #eee;">
            <td style="padding: 10px 0; color: #666; font-size: 13px;">Result</td>
            <td style="padding: 10px 0; font-size: 13px;">{score_line}</td>
          </tr>
          <tr style="border-top: 1px solid #eee;">
            <td style="padding: 10px 0; color: #666; font-size: 13px;">Completed at</td>
            <td style="padding: 10px 0; font-size: 13px;">{timestamp}</td>
          </tr>
          <tr style="border-top: 1px solid #eee;">
            <td style="padding: 10px 0; color: #666; font-size: 13px;">Run ID</td>
            <td style="padding: 10px 0; font-size: 13px; color: #999; font-family: monospace;">{run_id[:16]}…</td>
          </tr>
        </table>

        {"<p style='color: #555; font-size: 13px;'>The full HTML report is attached to this email.</p>" if report_html else ""}

        <p style="color: #999; font-size: 11px; margin-top: 32px; border-top: 1px solid #eee; padding-top: 16px;">
          You're receiving this because email notifications are enabled for your MetronAI account.
        </p>
      </div>

    </body>
    </html>
    """

    filename = f"metron_report_{agent_name.replace(' ', '_') or run_id[:8]}.html"

    _send_raw(
        to_email=user_email,
        subject=f"MetronAI — {agent_name or 'Run'} complete ({round(health_score * 100) if health_score else total_passed}/{total_tests})",
        body_html=body,
        attachment_html=report_html,
        attachment_filename=filename,
    )


def notify_run_failed(
    user_email: str,
    agent_name: str,
    run_id:     str,
    error_msg:  str,
) -> None:
    """Send a 'run failed' email with the error details."""

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    short_error = error_msg[:500] if error_msg else "Unknown error"

    body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #1a1a1a; max-width: 600px; margin: 0 auto;">

      <div style="background: linear-gradient(135deg, #ba1a1a, #8b0000); padding: 32px; border-radius: 12px 12px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 24px;">MetronAI</h1>
        <p style="color: rgba(255,255,255,0.8); margin: 8px 0 0 0; font-size: 14px;">AI Quality Intelligence Platform</p>
      </div>

      <div style="background: #f9f9f9; padding: 32px; border-radius: 0 0 12px 12px; border: 1px solid #e0e0e0;">
        <h2 style="color: #ba1a1a; margin-top: 0;">Your test run encountered an error ⚠️</h2>

        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
          <tr>
            <td style="padding: 10px 0; color: #666; font-size: 13px; width: 40%;">Agent</td>
            <td style="padding: 10px 0; font-weight: bold; font-size: 13px;">{agent_name or "—"}</td>
          </tr>
          <tr style="border-top: 1px solid #eee;">
            <td style="padding: 10px 0; color: #666; font-size: 13px;">Failed at</td>
            <td style="padding: 10px 0; font-size: 13px;">{timestamp}</td>
          </tr>
          <tr style="border-top: 1px solid #eee;">
            <td style="padding: 10px 0; color: #666; font-size: 13px;">Run ID</td>
            <td style="padding: 10px 0; font-size: 13px; color: #999; font-family: monospace;">{run_id[:16]}…</td>
          </tr>
        </table>

        <div style="background: #fff3f3; border: 1px solid #ffcccc; border-radius: 8px; padding: 16px; margin: 16px 0;">
          <p style="color: #ba1a1a; font-size: 12px; font-weight: bold; margin: 0 0 8px 0;">ERROR DETAILS</p>
          <p style="color: #555; font-size: 12px; font-family: monospace; margin: 0; word-break: break-word;">{short_error}</p>
        </div>

        <p style="color: #555; font-size: 13px;">
          Please check your endpoint configuration and try again. If the issue persists, contact your platform administrator.
        </p>

        <p style="color: #999; font-size: 11px; margin-top: 32px; border-top: 1px solid #eee; padding-top: 16px;">
          You're receiving this because email notifications are enabled for your MetronAI account.
        </p>
      </div>

    </body>
    </html>
    """

    _send_raw(
        to_email=user_email,
        subject=f"MetronAI — {agent_name or 'Run'} failed ⚠️",
        body_html=body,
    )

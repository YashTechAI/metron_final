"""
Cognito Admin API helpers — used by admin/super endpoints to invite and remove users.
Requires AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY in env with cognito-idp permissions.
"""

import os
import boto3
from botocore.exceptions import ClientError

_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
_REGION = os.environ.get("AWS_REGION") or os.environ.get("COGNITO_REGION", "us-east-1")


def _client():
    return boto3.client(
        "cognito-idp",
        region_name=_REGION,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )


def invite_user(email: str) -> dict:
    """
    Create a Cognito user and send them a temporary password via email.
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    try:
        _client().admin_create_user(
            UserPoolId=_USER_POOL_ID,
            Username=email,
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
            DesiredDeliveryMediums=["EMAIL"],
        )
        return {"ok": True}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "UsernameExistsException":
            try:
                _client().admin_create_user(
                    UserPoolId=_USER_POOL_ID,
                    Username=email,
                    UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
                    DesiredDeliveryMediums=["EMAIL"],
                    MessageAction="RESEND",
                )
                return {"ok": True}
            except ClientError as e2:
                return {"ok": False, "error": str(e2)}
        return {"ok": False, "error": str(e)}


def delete_user(email: str) -> dict:
    """
    Delete a Cognito user by email. Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    try:
        _client().admin_delete_user(UserPoolId=_USER_POOL_ID, Username=email)
        return {"ok": True}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "UserNotFoundException":
            return {"ok": True}  # Already gone — treat as success
        return {"ok": False, "error": str(e)}


def user_exists(email: str) -> bool:
    """Check if a user exists in Cognito."""
    try:
        _client().admin_get_user(UserPoolId=_USER_POOL_ID, Username=email)
        return True
    except ClientError:
        return False

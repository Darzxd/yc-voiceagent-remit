"""Cognito User Pool refresh -> Identity Pool IAM creds.

The Maximo backoffice frontend signs every call to its API Gateway with AWS
Sig V4. The IAM credentials it uses are SHORT-LIVED (≈1h) and come from a
Cognito Identity Pool, which exchanges a Cognito User Pool ``idToken`` for
temporary AWS creds. The user authenticates via password ONCE; after that,
the ``refreshToken`` (valid ~30 days) is enough to mint new id tokens.

This module replicates that flow programmatically from the bot:

    refresh_token  ─[cognito-idp:InitiateAuth REFRESH_TOKEN_AUTH]─>  id_token
    id_token       ─[cognito-identity:GetId]──────────────────────>  identity_id
    identity_id    ─[cognito-identity:GetCredentialsForIdentity]──>  IAM creds

Creds are cached in-memory and refreshed ~5 min before expiry so the calling
code never sees an expired key.

If the refresh token has been revoked (eg. user logged out / >30 days), all
calls return None and the bot's recarga falls back to mock.
"""

import asyncio
import datetime as dt
import json
import os
from dataclasses import dataclass

import aiohttp
from loguru import logger


@dataclass
class IamCreds:
    access_key: str
    secret_key: str
    session_token: str
    expires_at: dt.datetime  # UTC

    def expiring_soon(self, skew_seconds: int = 300) -> bool:
        return (self.expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds() < skew_seconds


_CACHED_CREDS: IamCreds | None = None
_CACHED_IDENTITY_ID: str | None = None
_LOCK = asyncio.Lock()


def _required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing env var {name}")
    return val


async def _refresh_id_token(refresh_token: str) -> str:
    """Exchange a refresh_token for a fresh idToken via Cognito User Pool."""
    region = _required("MAXIMO_AWS_REGION")
    url = f"https://cognito-idp.{region}.amazonaws.com/"
    body = {
        "AuthFlow": "REFRESH_TOKEN_AUTH",
        "ClientId": _required("MAXIMO_USER_POOL_CLIENT_ID"),
        "AuthParameters": {"REFRESH_TOKEN": refresh_token},
    }
    headers = {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = json.loads(await r.text())
            if r.status >= 300:
                raise RuntimeError(f"cognito-idp InitiateAuth failed {r.status}: {data}")
            return data["AuthenticationResult"]["IdToken"]


async def _get_identity_id(id_token: str) -> str:
    """Get the Cognito Identity Pool identity id (cached after first call)."""
    global _CACHED_IDENTITY_ID
    if _CACHED_IDENTITY_ID:
        return _CACHED_IDENTITY_ID
    region = _required("MAXIMO_AWS_REGION")
    pool_id = _required("MAXIMO_IDENTITY_POOL_ID")
    user_pool_id = _required("MAXIMO_USER_POOL_ID")
    url = f"https://cognito-identity.{region}.amazonaws.com/"
    body = {
        "IdentityPoolId": pool_id,
        "Logins": {f"cognito-idp.{region}.amazonaws.com/{user_pool_id}": id_token},
    }
    headers = {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityService.GetId",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = json.loads(await r.text())
            if r.status >= 300:
                raise RuntimeError(f"cognito-identity GetId failed {r.status}: {data}")
            _CACHED_IDENTITY_ID = data["IdentityId"]
            return _CACHED_IDENTITY_ID


async def _get_iam_creds(identity_id: str, id_token: str) -> IamCreds:
    region = _required("MAXIMO_AWS_REGION")
    user_pool_id = _required("MAXIMO_USER_POOL_ID")
    url = f"https://cognito-identity.{region}.amazonaws.com/"
    body = {
        "IdentityId": identity_id,
        "Logins": {f"cognito-idp.{region}.amazonaws.com/{user_pool_id}": id_token},
    }
    headers = {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = json.loads(await r.text())
            if r.status >= 300:
                raise RuntimeError(f"GetCredentialsForIdentity failed {r.status}: {data}")
            c = data["Credentials"]
            return IamCreds(
                access_key=c["AccessKeyId"],
                secret_key=c["SecretKey"],
                session_token=c["SessionToken"],
                expires_at=dt.datetime.fromtimestamp(c["Expiration"], tz=dt.timezone.utc),
            )


async def get_iam_creds() -> IamCreds | None:
    """Return valid IAM credentials, refreshing them automatically.

    Returns None on any failure — callers should fall back to mock recargas.
    """
    global _CACHED_CREDS
    if _CACHED_CREDS and not _CACHED_CREDS.expiring_soon():
        return _CACHED_CREDS
    async with _LOCK:
        if _CACHED_CREDS and not _CACHED_CREDS.expiring_soon():
            return _CACHED_CREDS
        try:
            refresh = _required("MAXIMO_REFRESH_TOKEN")
            id_token = await _refresh_id_token(refresh)
            identity_id = await _get_identity_id(id_token)
            _CACHED_CREDS = await _get_iam_creds(identity_id, id_token)
            logger.info(
                f"maximo_auth: refreshed IAM creds, valid until {_CACHED_CREDS.expires_at.isoformat()}"
            )
            return _CACHED_CREDS
        except Exception as e:
            logger.error(f"maximo_auth.get_iam_creds failed: {e}")
            return None

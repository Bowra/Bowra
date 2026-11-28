"""Verified Teams identities and member-confirmed account pairing."""

import asyncio
import time
from functools import lru_cache
from urllib.parse import urlencode
from uuid import UUID

import jwt
from fastapi import HTTPException

from bowhead_gateway.im.error_messages import im_interface_message
from bowhead_gateway.public_url import team_public_app_url
from bowhead_gateway.routes.auth import derive_jwt_secret_keys


@lru_cache(maxsize=32)
def _keys(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, timeout=5)


async def verify_connector(token, connection, activity):
    try:
        key = await asyncio.to_thread(_keys("https://login.botframework.com/v1/.well-known/keys").get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=connection.microsoft_app_id,
            issuer="https://api.botframework.com",
            options={"require": ["exp", "nbf"]},
        )
        if (
            activity.get("channelId") != "msteams"
            or not activity.get("serviceUrl")
            or claims.get("serviceurl", claims.get("serviceUrl")) != activity["serviceUrl"]
        ):
            raise ValueError("Connector channel or service URL mismatch")
        endorsements = key._jwk_data.get("endorsements")
        if not isinstance(endorsements, list) or "msteams" not in endorsements:
            raise HTTPException(403, {"error": {"code": "teams_channel_not_endorsed"}})
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(503, {"error": {"code": "teams_signing_keys_unavailable"}}, headers={"Retry-After": "5"}) from exc
    except (ValueError, jwt.PyJWTError) as exc:
        raise HTTPException(401, {"error": {"code": "teams_invalid_connector_token"}}) from exc


async def verify_sso(token, connection, tenant, activity):
    try:
        tenant_guid = str(UUID(tenant))
        key = await asyncio.to_thread(_keys(f"https://login.microsoftonline.com/{tenant_guid}/discovery/v2.0/keys").get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=[connection.sso_audience, connection.sso_auth_app_id or connection.microsoft_app_id],
            issuer=f"https://login.microsoftonline.com/{tenant_guid}/v2.0",
            options={"require": ["exp", "tid", "oid", "scp"]},
        )
        if (
            claims["tid"] != tenant
            or claims["oid"] != activity["from"]["aadObjectId"]
            or not isinstance(claims["scp"], str)
            or "access_as_user" not in claims["scp"].split()
        ):
            raise ValueError("SSO identity or scope mismatch")
        if (activity.get("value") or {}).get("connectionName") != connection.sso_connection_name:
            raise ValueError("SSO connection mismatch")
        return claims
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(503, {"error": {"code": "teams_signing_keys_unavailable"}}, headers={"Retry-After": "5"}) from exc
    except (KeyError, ValueError, TypeError, jwt.PyJWTError) as exc:
        raise HTTPException(401, {"error": {"code": "teams_invalid_sso_token"}}) from exc


async def pairing_card(request, connection, tenant, oid, user_id, locale):
    keys = derive_jwt_secret_keys("teams-pairing")
    if not keys:
        raise HTTPException(503, {"error": {"code": "teams_pairing_unavailable"}})
    token = jwt.encode(
        {
            "aud": "teams-pairing",
            "exp": int(time.time()) + 600,
            "sub": user_id,
            "tid": tenant,
            "oid": oid,
            "team": connection.team_id,
            "app": connection.microsoft_app_id,
        },
        keys[0][1],
        algorithm="HS256",
    )
    url = await team_public_app_url(request, team_id=connection.team_id, path="/me/integrations/teams/bind/")
    return {
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": [{"type": "TextBlock", "wrap": True, "text": im_interface_message("teams_pair_account", locale)}],
        "actions": [
            {"type": "Action.OpenUrl", "title": im_interface_message("teams_pair_title", locale), "url": url + "?" + urlencode({"pair": token})}
        ],
    }


def pairing_claims(token):
    for _, key in derive_jwt_secret_keys("teams-pairing"):
        try:
            return jwt.decode(
                token, key, algorithms=["HS256"], audience="teams-pairing", options={"require": ["exp", "sub", "tid", "oid", "team", "app"]}
            )
        except jwt.InvalidSignatureError:
            continue
        except jwt.PyJWTError:
            break
    raise HTTPException(400, {"error": {"code": "teams_pairing_link_expired"}})

"""Signed impact previews and idempotent protected-resource receipts.

The token is deliberately small and opaque to callers.  It binds one actor,
one resource, the canonical mutation command, the resource state observed by
preview, and the complete dependency-impact digest.  Mutation code must
recompute both digests inside its write transaction before applying a change.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if TYPE_CHECKING:
    import aiosqlite

_TOKEN_DOMAIN: Final = b"bowhead/resource-impact/v1"
_KEY_ID_DOMAIN: Final = b"bowhead/resource-impact/key-id/v1\0"
_FERNET_ROOT_ENV: Final = "BOWHEAD_FERNET_KEY"
_TOKEN_TTL_SECONDS: Final = 300


class ResourceImpactError(RuntimeError):
    code = "resource_impact_invalid"


class ResourceImpactKeyUnavailable(ResourceImpactError):
    code = "impact_token_unavailable"


class ResourceImpactTokenInvalid(ResourceImpactError):
    code = "impact_token_invalid"


class ResourceImpactTokenStale(ResourceImpactError):
    code = "resource_impact_stale"


class ResourceChangeIdempotencyConflict(ResourceImpactError):
    code = "idempotency_key_reused"


@dataclass(frozen=True, slots=True)
class VerifiedResourceImpact:
    command_digest: str
    resource_state_digest: str
    dependency_impact_digest: str


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest_json(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode()).hexdigest()}"


def resource_change_request_digest(
    *,
    resource_kind: str,
    resource_id: str,
    command: Any,
) -> str:
    """Bind an actor-scoped idempotency key to one exact resource request."""

    return digest_json(
        {
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "command": command,
        }
    )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(
            f"{value}{'=' * (-len(value) % 4)}".encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
        raise ResourceImpactTokenInvalid("invalid base64") from exc


def _signing_key(root_key: str | bytes | None = None) -> tuple[str, bytes]:
    configured = root_key if root_key is not None else os.environ.get(_FERNET_ROOT_ENV)
    if not configured:
        raise ResourceImpactKeyUnavailable("BOWHEAD_FERNET_KEY is required")
    raw = configured.encode("ascii") if isinstance(configured, str) else configured
    try:
        root = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise ResourceImpactKeyUnavailable("BOWHEAD_FERNET_KEY is invalid") from exc
    if len(root) != 32:
        raise ResourceImpactKeyUnavailable("BOWHEAD_FERNET_KEY must decode to 32 bytes")
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_TOKEN_DOMAIN,
    ).derive(root)
    key_id = hashlib.sha256(_KEY_ID_DOMAIN + key).hexdigest()[:16]
    return key_id, key


def issue_impact_token(
    *,
    team_id: str,
    actor_user_id: str,
    resource_kind: str,
    resource_id: str,
    command: Any,
    resource_state: Any,
    dependency_impact: Any,
    ttl_seconds: int = _TOKEN_TTL_SECONDS,
    now: int | None = None,
    root_key: str | bytes | None = None,
) -> str:
    key_id, key = _signing_key(root_key)
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "v": 1,
        "t": team_id,
        "u": actor_user_id,
        "rk": resource_kind,
        "r": resource_id,
        "c": digest_json(command),
        "s": digest_json(resource_state),
        "d": digest_json(dependency_impact),
        "iat": issued_at,
        "exp": issued_at + max(1, min(ttl_seconds, _TOKEN_TTL_SECONDS)),
    }
    encoded = _b64encode(canonical_json(payload).encode())
    signature = _b64encode(hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{key_id}.{encoded}.{signature}"


def verify_impact_token(
    token: str,
    *,
    team_id: str,
    actor_user_id: str,
    resource_kind: str,
    resource_id: str,
    command: Any,
    resource_state: Any,
    dependency_impact: Any,
    now: int | None = None,
    root_key: str | bytes | None = None,
) -> VerifiedResourceImpact:
    try:
        key_id, encoded, encoded_signature = token.split(".", 2)
        payload = json.loads(_b64decode(encoded))
        signature = _b64decode(encoded_signature)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, ResourceImpactTokenInvalid) as exc:
        raise ResourceImpactTokenInvalid("malformed impact token") from exc
    expected_key_id, key = _signing_key(root_key)
    expected_signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    if key_id != expected_key_id or not hmac.compare_digest(expected_signature, signature):
        raise ResourceImpactTokenInvalid("impact token signature mismatch")
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ResourceImpactTokenInvalid("unsupported impact token")
    current = int(time.time()) if now is None else int(now)
    try:
        expired = current >= int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ResourceImpactTokenInvalid("invalid token expiry") from exc
    identity = (payload.get("t"), payload.get("u"), payload.get("rk"), payload.get("r"))
    if identity != (team_id, actor_user_id, resource_kind, resource_id):
        raise ResourceImpactTokenInvalid("impact token scope mismatch")
    if expired:
        raise ResourceImpactTokenStale("impact token expired")
    command_digest = digest_json(command)
    state_digest = digest_json(resource_state)
    impact_digest = digest_json(dependency_impact)
    if payload.get("c") != command_digest or payload.get("s") != state_digest or payload.get("d") != impact_digest:
        raise ResourceImpactTokenStale("resource or dependency impact changed")
    return VerifiedResourceImpact(
        command_digest=command_digest,
        resource_state_digest=state_digest,
        dependency_impact_digest=impact_digest,
    )


async def read_change_receipt(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    actor_user_id: str,
    idempotency_key: str,
    request_digest: str,
) -> dict[str, Any] | None:
    await _prune_change_receipts(db)
    async with db.execute(
        "SELECT request_digest,response_json FROM resource_change_receipt WHERE team_id=? AND actor_user_id=? AND idempotency_key=?",
        (team_id, actor_user_id, idempotency_key),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    if str(row[0]) != request_digest:
        raise ResourceChangeIdempotencyConflict("Idempotency-Key was reused with another request")
    value = json.loads(str(row[1]))
    if not isinstance(value, dict):
        raise ResourceImpactError("stored receipt is invalid")
    return value


async def _prune_change_receipts(db: aiosqlite.Connection) -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    async with db.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='resource_file_cleanup_receipt'",
    ) as cursor:
        has_cleanup_ledger = await cursor.fetchone() is not None
    if has_cleanup_ledger:
        await db.execute(
            "DELETE FROM resource_change_receipt WHERE created_at<? AND NOT EXISTS("
            "SELECT 1 FROM resource_file_cleanup_receipt cleanup "
            "WHERE cleanup.team_id=resource_change_receipt.team_id "
            "AND cleanup.actor_user_id=resource_change_receipt.actor_user_id "
            "AND cleanup.idempotency_key=resource_change_receipt.idempotency_key "
            "AND cleanup.state='pending')",
            (cutoff,),
        )
        return
    await db.execute(
        "DELETE FROM resource_change_receipt WHERE created_at<?",
        (cutoff,),
    )


async def write_change_receipt(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    actor_user_id: str,
    idempotency_key: str,
    resource_kind: str,
    resource_id: str,
    action: str,
    request_digest: str,
    impact_digest: str,
    response: dict[str, Any],
) -> None:
    await _prune_change_receipts(db)
    response_json = canonical_json(response)
    if len(response_json) > 16384:
        raise ResourceImpactError("resource change receipt is too large")
    await db.execute(
        "INSERT INTO resource_change_receipt("
        "team_id,actor_user_id,idempotency_key,resource_kind,resource_id,action,"
        "request_digest,impact_digest,response_json,created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            team_id,
            actor_user_id,
            idempotency_key,
            resource_kind,
            resource_id,
            action,
            request_digest,
            impact_digest,
            response_json,
            datetime.now(UTC).isoformat(),
        ),
    )


__all__ = [
    "ResourceChangeIdempotencyConflict",
    "ResourceImpactError",
    "ResourceImpactKeyUnavailable",
    "ResourceImpactTokenInvalid",
    "ResourceImpactTokenStale",
    "VerifiedResourceImpact",
    "canonical_json",
    "digest_json",
    "issue_impact_token",
    "read_change_receipt",
    "resource_change_request_digest",
    "verify_impact_token",
    "write_change_receipt",
]

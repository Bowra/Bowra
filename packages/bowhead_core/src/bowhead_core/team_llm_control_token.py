"""Opaque, short-lived tokens for verification Plan and readiness decisions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from bowhead_core.team_llm_evidence import (
    canonical_json,
    control_token_key,
)


class ControlTokenError(ValueError):
    code = "invalid_confirmation_token"


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))
    except Exception as exc:
        raise ControlTokenError("malformed confirmation token") from exc


def issue_control_token(
    payload: dict[str, Any],
    *,
    purpose: str,
    expires_at_epoch: int,
    root_key: str | bytes | None = None,
) -> str:
    body = {
        **payload,
        "purpose": purpose,
        "exp": expires_at_epoch,
    }
    encoded = _encode(canonical_json(body).encode())
    signature = hmac.new(
        control_token_key(root_key=root_key),
        encoded.encode(),
        hashlib.sha256,
    ).digest()
    return f"{encoded}.{_encode(signature)}"


def verify_control_token(
    token: str,
    *,
    purpose: str,
    now_epoch: int | None = None,
    root_key: str | bytes | None = None,
) -> dict[str, Any]:
    try:
        encoded, supplied_signature = token.split(".", 1)
    except ValueError as exc:
        raise ControlTokenError("malformed confirmation token") from exc
    expected = hmac.new(
        control_token_key(root_key=root_key),
        encoded.encode(),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, _decode(supplied_signature)):
        raise ControlTokenError("confirmation token signature mismatch")
    try:
        payload = json.loads(_decode(encoded))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ControlTokenError("malformed confirmation token payload") from exc
    if not isinstance(payload, dict) or payload.get("purpose") != purpose:
        raise ControlTokenError("confirmation token purpose mismatch")
    expiry = payload.get("exp")
    effective_now = int(time.time()) if now_epoch is None else now_epoch
    if not isinstance(expiry, int) or expiry < effective_now:
        raise ControlTokenError("confirmation token expired")
    return payload

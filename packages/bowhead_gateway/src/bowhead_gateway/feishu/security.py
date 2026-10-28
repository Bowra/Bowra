"""Feishu event security helpers.

Supports the two common Feishu delivery modes:
- plaintext event bodies with verification token
- encrypted event bodies with X-Lark-Signature and encrypt_key
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


class FeishuSecurityError(ValueError):
    """Raised when a Feishu callback cannot be trusted or decoded."""


@dataclass(frozen=True)
class FeishuSecurityConfig:
    verification_token: str = ""
    encrypt_key: str = ""
    bot_open_id: str = ""
    group_public_answers_enabled: bool = False

    @property
    def token_configured(self) -> bool:
        return bool(self.verification_token)

    @property
    def encryption_configured(self) -> bool:
        return bool(self.encrypt_key)


def verify_signature(
    *,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    raw_body: bytes,
    encrypt_key: str,
) -> None:
    """Verify Feishu's request signature when Encrypt Key is configured."""

    if not encrypt_key:
        return
    if not (timestamp and nonce and signature):
        raise FeishuSecurityError("missing Feishu signature headers")
    base = f"{timestamp}{nonce}{encrypt_key}".encode() + raw_body
    expected = hashlib.sha256(base).hexdigest()
    if not hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8")):
        raise FeishuSecurityError("invalid Feishu signature")


def parse_callback_body(
    *,
    raw_body: bytes,
    headers: dict[str, str],
    config: FeishuSecurityConfig,
) -> dict[str, Any]:
    """Verify/decrypt a Feishu callback body and return the event JSON."""

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise FeishuSecurityError("invalid Feishu JSON body") from exc

    if "encrypt" in body:
        verify_signature(
            timestamp=headers.get("x-lark-request-timestamp"),
            nonce=headers.get("x-lark-request-nonce"),
            signature=headers.get("x-lark-signature"),
            raw_body=raw_body,
            encrypt_key=config.encrypt_key,
        )
        if not config.encrypt_key:
            raise FeishuSecurityError("encrypted callback received but encrypt key is not set")
        body = _decrypt_event_body(str(body["encrypt"]), config.encrypt_key)

    _verify_token(body, config)
    return body


def extract_challenge(body: dict[str, Any]) -> str | None:
    """Return the URL-verification challenge when this is a challenge request."""

    if body.get("type") == "url_verification" and body.get("challenge"):
        return str(body["challenge"])
    if body.get("challenge") and not body.get("event"):
        return str(body["challenge"])
    return None


def _verify_token(body: dict[str, Any], config: FeishuSecurityConfig) -> None:
    if not config.verification_token:
        return
    token = body.get("token")
    header = body.get("header")
    if not token and isinstance(header, dict):
        token = header.get("token")
    if not token:
        raise FeishuSecurityError("missing Feishu verification token")
    if not hmac.compare_digest(
        str(token).encode("utf-8"),
        config.verification_token.encode("utf-8"),
    ):
        raise FeishuSecurityError("invalid Feishu verification token")


def _decrypt_event_body(encrypted: str, encrypt_key: str) -> dict[str, Any]:
    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher,
            algorithms,
            modes,
        )
    except Exception as exc:
        raise FeishuSecurityError(
            "cryptography package is required for encrypted Feishu callbacks",
        ) from exc

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    try:
        decoded = base64.b64decode(encrypted)
    except Exception as exc:
        raise FeishuSecurityError("invalid Feishu encrypted body base64") from exc
    if len(decoded) <= 16:
        raise FeishuSecurityError("Feishu encrypted body is too short")
    iv = decoded[:16]
    ciphertext = decoded[16:]
    if len(ciphertext) % 16 != 0:
        raise FeishuSecurityError("Feishu encrypted body is not block aligned")
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        raise FeishuSecurityError("empty decrypted Feishu body")
    pad = padded[-1]
    if pad < 1 or pad > 16:
        raise FeishuSecurityError("invalid Feishu encrypted body padding")
    plain = padded[:-pad]
    try:
        return json.loads(plain.decode("utf-8"))
    except Exception as exc:
        raise FeishuSecurityError("decrypted Feishu body is not JSON") from exc

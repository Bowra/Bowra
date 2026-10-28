"""P4.5.2 — Team LLM config encrypt/decrypt (fernet, master key from env).

dev fallback: BOWHEAD_FERNET_KEY 未设 → 不加密 (warning), 仅 dev/test.
prod 必设 (32 byte url-safe base64).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet | None:
    key = os.environ.get("BOWHEAD_FERNET_KEY")
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        logger.warning("BOWHEAD_FERNET_KEY invalid, encryption disabled")
        return None


def encrypt_llm_config(config: dict[str, Any]) -> str:
    """Encrypt config dict → opaque token. dev fallback returns plain JSON."""
    raw = json.dumps(config, ensure_ascii=False).encode("utf-8")
    f = _get_fernet()
    if f is None:
        logger.warning("llm_config encrypt: no FERNET_KEY, storing plaintext (DEV ONLY)")
        return "PLAIN:" + raw.decode("utf-8")
    return "FERNET:" + f.encrypt(raw).decode("utf-8")


def decrypt_llm_config(stored: str) -> dict[str, Any]:
    """Decrypt → dict. Raises ValueError on bad token."""
    if not stored:
        return {}
    if stored.startswith("PLAIN:"):
        return json.loads(stored[6:])
    if stored.startswith("FERNET:"):
        f = _get_fernet()
        if f is None:
            raise ValueError("FERNET_KEY required to decrypt")
        try:
            raw = f.decrypt(stored[7:].encode())
            return json.loads(raw)
        except InvalidToken as e:
            raise ValueError(f"invalid fernet token (key rotation?): {e}") from e
    # Legacy / unknown prefix → try treating it as PLAIN.
    try:
        return json.loads(stored)
    except json.JSONDecodeError:
        return {}


def redact_for_display(config: dict[str, Any]) -> dict[str, Any]:
    """API key → `sk-***...****` (前 4 后 4) 给 admin UI display."""
    out = json.loads(json.dumps(config))  # deep copy
    for p in out.get("providers", []):
        k = p.get("api_key") or ""
        if k:
            p["api_key"] = (k[:4] + "***" + k[-4:]) if len(k) > 8 else "***"
    return out

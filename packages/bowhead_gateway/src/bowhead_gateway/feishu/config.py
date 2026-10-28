"""Feishu app configuration model and resolver."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from bowhead_core.llm_config_crypto import decrypt_llm_config

FeishuAppType = Literal["internal", "store"]
FeishuEventIngress = Literal["webhook", "long_connection", "disabled"]
LONG_CONNECTION_RESTART_FIELDS = frozenset({"enabled", "event_ingress", "app_id", "app_secret"})

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeishuAppConfig:
    app_type: FeishuAppType = "internal"
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""
    bot_open_id: str = ""
    enabled: bool = True
    group_public_answers_enabled: bool = False
    event_ingress: FeishuEventIngress = "webhook"

    def token_cache_key(self, tenant_key: str | None) -> tuple[str, str, str]:
        return (self.app_id, tenant_key or "", self.app_type)


def long_connection_restart_signature(app_config: FeishuAppConfig) -> str:
    return "|".join(
        [
            "1" if app_config.enabled else "0",
            app_config.event_ingress,
            app_config.app_id,
            hashlib.sha256(app_config.app_secret.encode("utf-8")).hexdigest()[:12] if app_config.app_secret else "",
        ]
    )


class FeishuAppConfigResolver(Protocol):
    def resolve(self, tenant_key: str | None = None) -> FeishuAppConfig:
        """Return the Feishu app config for a tenant."""


class EnvFeishuAppConfigResolver:
    """Phase 1 resolver backed by process settings for an internal app."""

    def __init__(self, settings: object) -> None:
        self._settings = settings

    def resolve(self, tenant_key: str | None = None) -> FeishuAppConfig:
        del tenant_key
        raw_ingress = str(getattr(self._settings, "feishu_event_ingress", "webhook") or "webhook")
        event_ingress: FeishuEventIngress
        if raw_ingress in {"webhook", "long_connection", "disabled"}:
            event_ingress = raw_ingress  # type: ignore[assignment]
        else:
            event_ingress = "webhook"
        return FeishuAppConfig(
            app_type="internal",
            app_id=str(getattr(self._settings, "feishu_app_id", "") or ""),
            app_secret=str(getattr(self._settings, "feishu_app_secret", "") or ""),
            verification_token=str(
                getattr(self._settings, "feishu_verification_token", "") or "",
            ),
            encrypt_key=str(getattr(self._settings, "feishu_encrypt_key", "") or ""),
            bot_open_id=str(getattr(self._settings, "feishu_bot_open_id", "") or ""),
            enabled=bool(getattr(self._settings, "feishu_enabled", False)),
            group_public_answers_enabled=bool(
                getattr(self._settings, "feishu_group_public_answers_enabled", False),
            ),
            event_ingress=event_ingress,
        )


class DbBackedFeishuAppConfigResolver:
    """Resolve DB-stored Feishu config overrides, falling back to env settings.

    The DB row may omit secret refs. In that case the corresponding env secret
    remains active, so admins can change non-secret policy without re-entering
    App Secret.
    """

    def __init__(self, settings: object, db_path: str) -> None:
        self._settings = settings
        self._db_path = db_path
        self._env = EnvFeishuAppConfigResolver(settings)
        self._cache: dict[str, FeishuAppConfig] = {}

    def resolve(self, tenant_key: str | None = None) -> FeishuAppConfig:
        cache_key = tenant_key or ""
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        fallback = self._env.resolve(tenant_key)
        row = self._lookup_row(cache_key)
        if row is None:
            self._cache[cache_key] = fallback
            return fallback
        app_type = str(row.get("app_type") or fallback.app_type)
        raw_ingress = str(row.get("event_ingress") or fallback.event_ingress)
        event_ingress: FeishuEventIngress = (
            cast("FeishuEventIngress", raw_ingress) if raw_ingress in {"webhook", "long_connection", "disabled"} else fallback.event_ingress
        )
        resolved = FeishuAppConfig(
            app_type=cast("FeishuAppType", app_type if app_type in {"internal", "store"} else "internal"),
            app_id=str(row.get("app_id") or fallback.app_id),
            app_secret=_secret_or_fallback(row.get("app_secret_ref"), fallback.app_secret),
            verification_token=_secret_or_fallback(
                row.get("verification_token_ref"),
                fallback.verification_token,
            ),
            encrypt_key=_secret_or_fallback(row.get("encrypt_key_ref"), fallback.encrypt_key),
            bot_open_id=str(row.get("bot_open_id") or fallback.bot_open_id),
            enabled=bool(row.get("enabled")),
            group_public_answers_enabled=bool(row.get("group_public_answers_enabled")),
            event_ingress=event_ingress,
        )
        self._cache[cache_key] = resolved
        return resolved

    def invalidate(self, tenant_key: str | None = None) -> None:
        if tenant_key is None:
            self._cache.clear()
            return
        if not (tenant_key or "").strip():
            self._cache.clear()
            return
        self._cache.pop(tenant_key or "", None)

    def _lookup_row(self, tenant_key: str) -> dict[str, Any] | None:
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT * FROM feishu_app_configs "
                    "WHERE app_type='internal' AND tenant_key IN (?, '') "
                    "ORDER BY CASE WHEN tenant_key=? THEN 0 ELSE 1 END "
                    "LIMIT 1",
                    (tenant_key, tenant_key),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.debug("feishu.db_config_lookup_failed err=%s", exc)
            return None
        return dict(row) if row else None


def _secret_or_fallback(stored_ref: object, fallback: str) -> str:
    ref = str(stored_ref or "")
    if not ref:
        return fallback
    try:
        value = decrypt_llm_config(ref).get("value")
    except Exception as exc:
        logger.warning("feishu.secret_decrypt_failed err=%s", exc)
        return ""
    return str(value or "")

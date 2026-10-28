"""email-config sprint M1c — team-level SMTP 配置 admin 端点.

设计:docs/sprints/email-config-2026-06-12/tech-design.md §6b。

GET    /api/v1/admin/email-config        — 读(密码只 fingerprint;env 态无细节)
PUT    /api/v1/admin/email-config        — 写(密码留空=保留;校验+PLAIN gate;audit 无密码)
DELETE /api/v1/admin/email-config        — 清 team 配置回落 env
POST   /api/v1/admin/email-config/test   — 给调用者本人发测试信(限速;返 source;脱敏)

Security:
- team_admin gate(creator/team_admin)+ require_not_readonly。
- PLAIN: gate(无 FERNET_KEY 时拒明文写,mirror admin_billing._reject_plaintext_full_config)。
- audit payload 永不含 password;test error 只返回稳定 code + catalog 安全文案，
  SMTP/relay 原始诊断只留服务端日志。
- local 与 SaaS 部署均挂载；SMTP 目标地址按部署模式走共享出站策略
  (`validate_smtp_target`:SaaS 仅公网地址 + SMTP 标准端口)。BOWHEAD_TEAM_EMAIL_CONFIG
  关闭时端点不存在。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from bowhead_core.audit_actions import AuditAction
from bowhead_core.email_config import (
    deliver_email,
    env_smtp_config,
    resolve_smtp_config,
    team_email_config_enabled,
    validate_smtp_settings,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.error_handler import localize_error_message, resolve_terminal_error_language
from bowhead_gateway.product_identity import product_name
from bowhead_gateway.routes.auth import _require_team_role_dep, _store

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

logger = logging.getLogger(__name__)

_REQUIRE_TEAM_ADMIN = _require_team_role_dep("owner", "team_admin")


router = APIRouter()

# Empty password on write is the keep-old-value sentinel (empty string).
_KEEP = ""


class EmailConfigBody(BaseModel):
    host: str = Field(..., max_length=255)
    port: int = Field(587, ge=1, le=65535)
    user: str = Field("", max_length=320)
    password: str = Field("", max_length=512)  # Empty keeps old password.
    from_addr: str = Field(..., max_length=320)
    from_name: str = Field("", max_length=128)
    tls: bool = True


def _reject_plaintext(config: dict[str, Any]) -> None:
    """mirror admin_billing._reject_plaintext_full_config:无 FERNET_KEY 时拒明文落库。

    crypto 模块本身静默 PLAIN: 降级 + log,真正的 gate 在 route 层(admin_billing.py:294)。
    """
    from bowhead_core.llm_config_crypto import encrypt_llm_config

    token = encrypt_llm_config(config)
    if token.startswith("PLAIN:") and os.environ.get("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG") != "1":
        raise HTTPException(
            500,
            {"code": "email_config_encryption_required"},
        )


@router.get("")
async def get_email_config(
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    """配置 + 生效来源。密码只回 fingerprint;env 态不泄露 host 等细节(多租户)。"""
    cfg, version = await store.get_team_email_config_with_version(caller.team_id)
    if cfg and str(cfg.get("host") or "").strip():
        return {
            "configured": "team",
            "version": version,
            "config": {
                "host": cfg.get("host"),
                "port": cfg.get("port", 587),
                "user": cfg.get("user", ""),
                "from_addr": cfg.get("from_addr", ""),
                "from_name": cfg.get("from_name", ""),
                "tls": bool(cfg.get("tls", True)),
                # review-fix(security): return boolean only, never fingerprint; human-chosen SMTP passwords have low entropy,
                # making sha256 prefixes vulnerable to offline dictionary attacks. FE only needs to know whether one is set.
                "password_set": bool(cfg.get("password")),
            },
        }
    # Unconfigured team: reveal only whether an instance default exists (boolean), no env details.
    if env_smtp_config() is not None:
        return {"configured": "env", "version": version}
    return {"configured": "none", "version": version}


@router.put("", dependencies=[Depends(require_not_readonly)])
async def put_email_config(
    body: EmailConfigBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    # Empty password -> read/preserve old config (avoid echoing password in UI).
    password = body.password
    if password == _KEEP:
        old = await store.get_team_email_config_decrypted(caller.team_id)
        password = str(old.get("password") or "")

    config = {
        "host": body.host.strip(),
        "port": body.port,
        "user": body.user,
        "password": password,
        "from_addr": body.from_addr.strip(),
        "from_name": body.from_name,
        "tls": body.tls,
    }

    # Validate (synchronous getaddrinfo -> to_thread, avoiding event-loop blocking).
    error_codes = await asyncio.to_thread(validate_smtp_settings, config)
    if error_codes:
        issues = [
            {
                "code": code,
                "message": await localize_error_message(request, code),
            }
            for code in error_codes
        ]
        raise HTTPException(
            422,
            {"code": "invalid_smtp_config", "issues": issues},
        )

    _reject_plaintext(config)  # PLAIN gate (missing FERNET_KEY -> 500).
    await store.update_team_email_config(caller.team_id, config)

    # Audit payload never includes password (tech-design security #4).
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action=AuditAction.EMAIL_CONFIG_SET,
        target_kind="team",
        target_id=caller.team_id,
        scope="team",
        payload={
            "host": config["host"],
            "port": config["port"],
            "from_addr": config["from_addr"],
            "from_name": config["from_name"],
            "tls": config["tls"],
            "password_changed": body.password != _KEEP,
        },
        ip=request.client.host if request.client else None,
    )
    return {"ok": True}


@router.delete("", dependencies=[Depends(require_not_readonly)])
async def delete_email_config(
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    await store.clear_team_email_config(caller.team_id)
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action=AuditAction.EMAIL_CONFIG_CLEARED,
        target_kind="team",
        target_id=caller.team_id,
        scope="team",
        payload={},
        ip=request.client.host if request.client else None,
    )
    return {"ok": True}


@router.post("/test", dependencies=[Depends(require_not_readonly)])
async def test_email_config(
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    """Send a rate-limited test email to the caller and return safe diagnostics."""
    allowed = await store.rate_limit_check(
        f"email_test:team:{caller.team_id}",
        limit=1,
        window_s=2,
    )
    if not allowed:
        raise HTTPException(429, {"code": "rate_limited"})

    cfg = await resolve_smtp_config(caller.team_id, store)
    if cfg is None:
        raise HTTPException(422, {"code": "email_not_configured"})

    name = product_name()
    if resolve_terminal_error_language(request.scope) == "zh-CN":
        subject = f"{name} 邮件配置测试"
        body = f"这是一封来自 {name} 的测试邮件 —— 收到即表示 SMTP 配置可用。\n\n— {name}\n"
    else:
        subject = f"{name} email configuration test"
        body = f"This test email from {name} confirms that your SMTP configuration works.\n\n— {name}\n"
    ok, err = await deliver_email(
        caller.email,
        subject,
        body,
        config=cfg,
        timeout=20.0,
    )
    # Success -> requeue this team's dead rows stranded by earlier configuration errors.
    if ok:
        try:
            n = await store.requeue_dead_smtp(caller.team_id)
            if n:
                logger.info("email_test.requeued_dead team_id=%s n=%d", caller.team_id, n)
        except Exception as e:
            logger.warning("email_test.requeue_failed team_id=%s err=%s", caller.team_id, e)

    # Delivery diagnostics stay in server logs; response has stable code + safe copy in current locale.
    safe_err: str | None = None
    error_code: str | None = None
    if not ok:
        logger.warning(
            "email_test.delivery_failed team_id=%s source=%s err=%s",
            caller.team_id,
            cfg.source,
            err,
        )
        error_code = "email_test_env_delivery_failed" if cfg.source == "env" else "email_test_delivery_failed"
        safe_err = await localize_error_message(request, error_code)

    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action=AuditAction.EMAIL_TEST_SENT,
        target_kind="team",
        target_id=caller.team_id,
        scope="team",
        payload={"ok": ok, "source": cfg.source},  # Excludes raw error text.
        ip=request.client.host if request.client else None,
    )
    return {
        "ok": ok,
        "source": cfg.source,
        "error_code": error_code,
        "error": safe_err,
    }


def email_config_router_enabled() -> bool:
    """main.py mount gate：团队邮件服务开关启用(与部署模式无关)。"""
    return team_email_config_enabled()

"""email-config sprint — team-level SMTP 配置解析 + 统一投递 chokepoint.

设计:docs/sprints/email-config-2026-06-12/tech-design.md

两个职责:
1. resolve_smtp_config(team_id, store) — team-level BYO(Fernet 密文)优先,env BOWHEAD_SMTP_*
   兜底,两者皆无返 None(与历史 env-only 行为一致)。镜像 team_llm_config 房子模式。
2. deliver_email(...) — 唯一异步投递入口:asyncio.wait_for(to_thread(_send_smtp_detailed))。
   全部 7 条发信路径(direct + outbox worker + test 端点)都经它,smtplib 同步 I/O 永不在
   单事件循环里阻塞(tech-design §0.5:无鉴权的 forgot-password 路径尤其关键)。

写时校验 validate_smtp_settings:CRLF/控制字符拒绝(防 header 注入)+ from 格式；
目标地址按部署模式走共享出站策略 `validate_smtp_target`(SaaS:公网地址 + SMTP 标准端口；
local:全放行)。团队配置投递时在 `_send_smtp_detailed` 再验一次并连到已验证的 IP(防 DNS 重绑)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bowhead_core.ssrf_guard import SmtpPortBlocked, SsrfBlocked, validate_smtp_target

if TYPE_CHECKING:
    from bowhead_core.email_sender import EmailAttachment
    from bowhead_core.user_store import UserStore

logger = logging.getLogger(__name__)

_CTRL_CHARS = "\r\n\t\0\v\f"

# Flag (default ON): off → resolver uses only env (mount-when-on admin endpoints are also absent).
_FLAG_ENV = "BOWHEAD_TEAM_EMAIL_CONFIG"
SMTP_DEFAULTS_PATH = Path(__file__).with_name("email-defaults.json")


class EmailConfigError(Exception):
    """team 配置存在但解析/解密失败。strict_team=True(outbox worker)时抛出 → 行走
    failed_retry 可见失败,不静默换 env relay 发出(tech-design §6a / security #11)。"""


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    from_addr: str
    from_name: str = ""
    tls: bool = True
    source: str = "env"  # "team" | "env" — echoed by test endpoint and used for worker decisions


def team_email_config_enabled() -> bool:
    return (os.environ.get(_FLAG_ENV) or "1") == "1"


def _default_from_address() -> str:
    """Read the fixed package default; Community requires an operator sender."""
    defaults = json.loads(SMTP_DEFAULTS_PATH.read_text(encoding="utf-8"))
    if (
        not isinstance(defaults, dict)
        or set(defaults) != {"schema_version", "from_addr"}
        or type(defaults["schema_version"]) is not int
        or defaults["schema_version"] != 1
        or not isinstance(defaults["from_addr"], str)
        or any(c in defaults["from_addr"] for c in _CTRL_CHARS)
    ):
        raise ValueError("Unsupported packaged email defaults")
    return defaults["from_addr"].strip()


def env_smtp_config() -> SmtpConfig | None:
    """平移 email_sender._smtp_config() 语义:host 缺失 → None;TLS 默认 1。"""
    host = (os.environ.get("BOWHEAD_SMTP_HOST") or "").strip()
    if not host:
        return None
    return SmtpConfig(
        host=host,
        port=int(os.environ.get("BOWHEAD_SMTP_PORT") or 587),
        user=os.environ.get("BOWHEAD_SMTP_USER") or "",
        password=os.environ.get("BOWHEAD_SMTP_PASS") or "",
        from_addr=(os.environ.get("BOWHEAD_SMTP_FROM") or "").strip() or _default_from_address(),
        from_name=os.environ.get("BOWHEAD_SMTP_FROM_NAME") or "",
        tls=(os.environ.get("BOWHEAD_SMTP_TLS") or "1") == "1",
        source="env",
    )


def _config_from_dict(d: dict[str, Any]) -> SmtpConfig:
    return SmtpConfig(
        host=str(d.get("host") or "").strip(),
        port=int(d.get("port") or 587),
        user=str(d.get("user") or ""),
        password=str(d.get("password") or ""),
        from_addr=str(d.get("from_addr") or "").strip() or _default_from_address(),
        from_name=str(d.get("from_name") or ""),
        tls=bool(d.get("tls", True)),
        source="team",
    )


async def resolve_smtp_config(
    team_id: str | None,
    store: UserStore | None,
    *,
    strict_team: bool = False,
) -> SmtpConfig | None:
    """team 配置(存在)→ env 兜底 → None。

    flag off / team_id None / store None / team 无配置 → env_smtp_config()。
    team 配置存在但解析失败:
      strict_team=False(direct 路径)→ log + fallback env(发信路径不抛);
      strict_team=True(outbox worker)→ raise EmailConfigError(行走 failed_retry)。
    """
    if not team_email_config_enabled() or not team_id or store is None:
        return env_smtp_config()
    try:
        raw = await store.get_team_email_config_decrypted(team_id)
    except Exception as e:
        if strict_team:
            raise EmailConfigError(f"team email config unreadable: {e}") from e
        logger.warning("email_config.team_unreadable team_id=%s err=%s — fallback env", team_id, e)
        return env_smtp_config()
    if raw and str(raw.get("host") or "").strip():
        return _config_from_dict(raw)
    return env_smtp_config()


async def resolve_team_smtp_config(
    team_id: str | None,
    store: UserStore | None,
) -> SmtpConfig | None:
    """Resolve only the current team's saved SMTP service.

    This intentionally does not fall back to deployment-level environment
    settings: user-initiated outbound mail must be enabled explicitly by the
    team in its email-service settings.
    """
    if not team_email_config_enabled() or not team_id or store is None:
        return None
    raw = await store.get_team_email_config_decrypted(team_id)
    if not raw or not str(raw.get("host") or "").strip():
        return None
    return _config_from_dict(raw)


def _has_ctrl(v: str) -> bool:
    return any(c in v for c in _CTRL_CHARS)


def validate_smtp_settings(cfg: dict[str, Any]) -> list[str]:
    """写时校验,返稳定错误码列表(空=过,不抛)。tech-design §2。

    错误码由 Gateway/runtime catalog 和前端 next-intl 分别渲染；这里不能返回
    写死语言。
    """
    errors: list[str] = []
    host = str(cfg.get("host") or "").strip()
    from_addr = str(cfg.get("from_addr") or "").strip()
    from_name = str(cfg.get("from_name") or "")
    user = str(cfg.get("user") or "")

    # CRLF/control characters: prevent header injection ("X\r\nBcc: ..." can smuggle headers through MIMEText compat32).
    for val in (host, from_addr, from_name, user):
        if _has_ctrl(val):
            errors.append("smtp_control_characters")

    if not host:
        errors.append("smtp_host_required")
    elif not all(c.isalnum() or c in ".-" for c in host):
        # smtplib accepts underscores, etc., but urlsplit misparses them → reject (tech-design §2 #7).
        errors.append("smtp_host_invalid")

    if "@" not in from_addr:
        errors.append("smtp_from_address_invalid")

    # review-fix(P1): login supplied without password almost certainly means omission (SMTP AUTH needs a password); otherwise
    # configuration appears saved but every send becomes an SMTPAuthError dead letter. The put route merges the old password before validation,
    # so blank-to-preserve remains unaffected; only genuinely empty initial passwords are blocked here.
    if str(cfg.get("user") or "") and not str(cfg.get("password") or ""):
        errors.append("smtp_password_required")

    # Only the shared outbound policy determines target-address policy by deployment mode (CLAUDE.md, Outbound addresses and deployment modes).
    if host and "smtp_host_invalid" not in errors and "smtp_control_characters" not in errors:
        try:
            port = int(cfg.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        try:
            validate_smtp_target(host, port)
        except SmtpPortBlocked:
            errors.append("smtp_port_unsupported")
        except (SsrfBlocked, ValueError):
            errors.append("smtp_host_blocked")

    return list(dict.fromkeys(errors))


async def deliver_email(
    to: str,
    subject: str,
    body: str,
    *,
    config: SmtpConfig | None,
    timeout: float = 15.0,
    html_body: str | None = None,
    attachments: tuple[EmailAttachment, ...] = (),
) -> tuple[bool, str | None]:
    """唯一异步投递入口(tech-design §0.5)。smtplib 同步 I/O 全进 to_thread + wait_for
    timeout 兜底 → 黑洞/错配 host 不冻结单事件循环。返 (ok, error)。"""
    from bowhead_core.email_sender import _send_smtp_detailed

    if config is None:
        return False, "smtp not configured"
    send_kwargs = {"config": config}
    if html_body is not None:
        send_kwargs["html_body"] = html_body
    if attachments:
        send_kwargs["attachments"] = attachments
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _send_smtp_detailed,
                to,
                subject,
                body,
                **send_kwargs,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("email.deliver_timeout to=%s subject=%s timeout=%ss", to, subject, timeout)
        return False, f"smtp_timeout_{int(timeout)}s"

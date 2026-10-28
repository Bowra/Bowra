"""P4.5.9 — SMTP email sender for verify / password-reset / invite.

env config:
  BOWHEAD_SMTP_HOST=smtp.example.com
  BOWHEAD_SMTP_PORT=587
  BOWHEAD_SMTP_USER=...
  BOWHEAD_SMTP_PASS=...
  BOWHEAD_SMTP_FROM=no-reply@example.com  (use a sender your relay authorizes)
  BOWHEAD_SMTP_TLS=1  (default 1)

dev fallback: 没 host → log warning + 不发, return token (验测试).

Verify token: signed JWT exp 24h.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import time
from dataclasses import dataclass
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import TYPE_CHECKING

import jwt

if TYPE_CHECKING:
    from bowhead_core.email_config import SmtpConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    mime: str
    content: bytes


def _send_smtp_detailed(
    to: str,
    subject: str,
    body: str,
    *,
    config: SmtpConfig | None = None,
    html_body: str | None = None,
    attachments: tuple[EmailAttachment, ...] = (),
) -> tuple[bool, str | None]:
    """底层同步发送(在 to_thread 内跑)。返 (ok, error)。

    config 省略 → 回落 env(email-config sprint 前的现行为,既有调用者/UT 不破)。
    错误细节版为主实现:test 端点需要失败原因(_send_smtp 仅取 [0] 保旧 bool 契约)。
    """
    cfg = config
    if cfg is None:
        from bowhead_core.email_config import env_smtp_config

        cfg = env_smtp_config()
    if cfg is None:
        logger.warning("smtp.no_config not_sending to=%s subject=%s", to, subject)
        return False, "smtp not configured"
    if not cfg.from_addr.strip():
        return False, "smtp sender not configured"
    if html_body is None:
        msg = MIMEText(body, "plain", "utf-8")
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    if attachments:
        mixed = MIMEMultipart("mixed")
        mixed.attach(msg)
        for attachment in attachments:
            maintype, subtype = attachment.mime.split("/", 1)
            part = MIMEBase(maintype, subtype)
            part.set_payload(attachment.content)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=attachment.filename)
            mixed.attach(part)
        msg = mixed
    msg["Subject"] = subject
    # An f-string with a Chinese from_name creates a malformed header; use formataddr + Header encoding (tech-design §3 #11).
    msg["From"] = formataddr((cfg.from_name, cfg.from_addr)) if cfg.from_name else cfg.from_addr
    msg["To"] = to
    # Team configuration is a user-supplied outbound address: revalidate by deployment mode before delivery (DNS may have changed),
    # and pin the connection to an IP validated now (prevent DNS rebinding). Deployment-provided instance env config is outside this scope.
    pinned_ips: tuple[str, ...] = ()
    if cfg.source == "team":
        from bowhead_core.ssrf_guard import SsrfBlocked, validate_smtp_target

        try:
            pinned_ips = validate_smtp_target(cfg.host, cfg.port).ips
        except (SsrfBlocked, ValueError) as exc:
            logger.warning("smtp.target_blocked to=%s err=%s", to, exc)
            return False, "smtp_target_blocked"
    try:
        server = _open_smtp(cfg, pinned_ips)
        if cfg.user:
            server.login(cfg.user, cfg.password)
        server.send_message(msg)
        server.quit()
        return True, None
    except Exception as e:
        logger.warning("smtp.send_failed to=%s err=%s", to, e)
        return False, f"{type(e).__name__}: {e}"


class _PinnedSocketMixin:
    """Connect to a pre-validated IP while TLS/SNI keeps using the configured host name."""

    _pinned_ip: str

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return super()._get_socket(self._pinned_ip, port, timeout)  # type: ignore[misc]


class _PinnedSMTP(_PinnedSocketMixin, smtplib.SMTP):
    def __init__(self, host: str, port: int, *, ip: str, timeout: float) -> None:
        self._pinned_ip = ip
        super().__init__(host, port, timeout=timeout)


class _PinnedSMTPSSL(_PinnedSocketMixin, smtplib.SMTP_SSL):
    def __init__(self, host: str, port: int, *, ip: str, timeout: float) -> None:
        self._pinned_ip = ip
        super().__init__(host, port, timeout=timeout)


def _open_smtp(cfg: SmtpConfig, pinned_ips: tuple[str, ...]) -> smtplib.SMTP:
    """打开 SMTP 会话。pinned_ips 非空时逐个尝试已验证的地址,不再按主机名重新解析。"""
    # review-fix(P1): port 465 uses implicit TLS (SMTPS), requiring a direct SMTP_SSL handshake, never
    # SMTP+starttls (plaintext to an SSL port stalls until timeout). Chinese enterprise mail providers
    # (QQ/163/Aliyun exmail) default to 465. 587/25 use STARTTLS (controlled by cfg.tls).
    implicit_tls = cfg.port == 465
    if not pinned_ips or pinned_ips == (cfg.host,):
        server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=10) if implicit_tls else smtplib.SMTP(cfg.host, cfg.port, timeout=10)
    else:
        last_error: OSError | None = None
        server = None
        for ip in pinned_ips:
            try:
                cls = _PinnedSMTPSSL if implicit_tls else _PinnedSMTP
                server = cls(cfg.host, cfg.port, ip=ip, timeout=10)
                break
            except OSError as exc:
                last_error = exc
        if server is None:
            assert last_error is not None
            raise last_error
    if not implicit_tls and cfg.tls:
        server.starttls()
    return server


def _send_smtp(to: str, subject: str, body: str, *, config: SmtpConfig | None = None) -> bool:
    """旧契约薄封装(bool)。唯一 SMTP socket 打开点仍是 _send_smtp_detailed。"""
    return _send_smtp_detailed(to, subject, body, config=config)[0]


def make_verify_token(
    user_id: str,
    email: str,
    secret: str,
    ttl_hours: int = 24 * 7,  # Sprint α r4 UX P0-U-4: 7d (was 24h)
    jti: str | None = None,  # Sprint α r4 Sec P1-S-7: anti-replay claim
) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "purpose": "email_verify",
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_hours * 3600,
    }
    if jti:
        payload["jti"] = jti
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_verify_token(token: str, secret: str | list[tuple[str, str]] | list[str]) -> dict | None:
    """Decode verify token. Accepts single secret OR list of secrets (for rotation).

    Sprint α P0-T-2 fix: support multi-key decode so JWT secret rotation doesn't
    invalidate outstanding 7-day verify tokens. Pass `_jwt_secret_keys()` list
    (list of (kid, key) tuples) or a list of plain keys. First-match wins.
    """
    # Normalize secrets to list of plain key strings
    if isinstance(secret, str):
        keys: list[str] = [secret]
    else:
        keys = [s[1] if isinstance(s, tuple) else s for s in secret]

    for k in keys:
        try:
            payload = jwt.decode(token, k, algorithms=["HS256"])
            if payload.get("purpose") != "email_verify":
                return None
            return payload
        except jwt.PyJWTError:
            continue
    return None


def make_password_reset_token(user_id: str, secret: str, ttl_hours: int = 24) -> str:
    payload = {
        "sub": user_id,
        "purpose": "password_reset",
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_hours * 3600,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_password_reset_token(token: str, secret: str) -> dict | None:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        if payload.get("purpose") != "password_reset":
            return None
        return payload
    except jwt.PyJWTError:
        return None

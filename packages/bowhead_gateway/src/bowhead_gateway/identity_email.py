"""Revision-pinned branding for product identity emails."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from html import escape
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from bowhead_core.theme_compiler import product_default_compiled_scheme

from bowhead_gateway.product_identity import product_name
from bowhead_gateway.public_branding import resolve_public_branding_bundle

# Fallback button colors when no team branding resolves: the product default light scheme.
_PRODUCT_DEFAULT_LIGHT = product_default_compiled_scheme()["light"]
_DEFAULT_ACCENT = _PRODUCT_DEFAULT_LIGHT["accent"]
_DEFAULT_ACCENT_INK = _PRODUCT_DEFAULT_LIGHT["accentInk"]

if TYPE_CHECKING:
    from bowhead_core.user_store import UserStore
    from fastapi import Request

IdentityEmailKind = Literal[
    "verify",
    "team_invite",
    "workspace_invite",
    "password_reset",
]


@dataclass(frozen=True, slots=True)
class IdentityEmailBrandSnapshot:
    team_id: str
    public_entry_id: str | None
    brand_revision_id: str | None
    brand_revision_checksum: str | None
    application_name: str
    logo_url: str | None
    public_base_url: str
    accent: str
    accent_ink: str
    recipe_version: str
    locale: str

    def to_json(self) -> str:
        return json.dumps(
            asdict(self),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class RenderedIdentityEmail:
    subject: str
    text_body: str
    html_body: str
    action_url: str
    snapshot: IdentityEmailBrandSnapshot


async def render_identity_email(
    request: Request,
    *,
    team_id: str,
    kind: IdentityEmailKind,
    action_path: str,
    inviter_name: str | None = None,
    team_name: str | None = None,
    resend: bool = False,
    locale: str | None = None,
    public_entry_id: str | None = None,
) -> RenderedIdentityEmail:
    """Resolve and render once; the outbox persists this exact result."""

    if locale is None:
        from bowhead_gateway.error_handler import resolve_terminal_error_language

        locale = "zh-CN" if resolve_terminal_error_language(request.scope) == "zh-CN" else "en-US"
    snapshot = await _resolve_snapshot(request, team_id, locale=locale, public_entry_id=public_entry_id)
    action_url = _entry_url(snapshot.public_base_url, action_path)
    application_name = snapshot.application_name
    subject, lead, action_label, expiry, footer = _copy(
        kind,
        application_name=application_name,
        inviter_name=inviter_name,
        team_name=team_name,
        resend=resend,
        locale=snapshot.locale,
    )
    text_body = f"{lead}\n\n{action_label}:\n{action_url}\n\n{expiry}\n\n{footer}\n\n— {application_name}\n"
    logo = (
        f'<img src="{escape(snapshot.logo_url, quote=True)}" '
        f'alt="{escape(application_name, quote=True)}" '
        'style="display:block;max-width:180px;max-height:52px;margin:0 0 24px">'
        if snapshot.logo_url
        else (f'<div style="font:600 22px/1.2 system-ui,sans-serif;color:#231f1b;margin:0 0 24px">{escape(application_name)}</div>')
    )
    html_body = (
        '<!doctype html><html><body style="margin:0;background:#f6f4ef;'
        'font:14px/1.6 system-ui,sans-serif;color:#231f1b">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
        '<tr><td align="center" style="padding:32px 16px">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="max-width:560px;background:#fff;border:1px solid #e2ddd3;'
        'border-radius:12px"><tr><td style="padding:32px">'
        f"{logo}<p>{escape(lead)}</p>"
        f'<p><a href="{escape(action_url, quote=True)}" '
        f'style="display:inline-block;background:{snapshot.accent};color:{snapshot.accent_ink};'
        'text-decoration:none;border-radius:6px;padding:10px 16px">'
        f"{escape(action_label)}</a></p>"
        f'<p style="color:#6f675e">{escape(expiry)}<br>{escape(footer)}</p>'
        f'<p style="color:#8a8178">— {escape(application_name)}</p>'
        "</td></tr></table></td></tr></table></body></html>"
    )
    return RenderedIdentityEmail(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        action_url=action_url,
        snapshot=snapshot,
    )


async def enqueue_identity_email(
    request: Request,
    store: UserStore,
    *,
    team_id: str,
    to_email: str,
    purpose: str,
    kind: IdentityEmailKind,
    action_path: str,
    inviter_name: str | None = None,
    team_name: str | None = None,
    resend: bool = False,
    locale: str | None = None,
    public_entry_id: str | None = None,
) -> RenderedIdentityEmail:
    rendered = await render_identity_email(
        request,
        team_id=team_id,
        kind=kind,
        action_path=action_path,
        inviter_name=inviter_name,
        team_name=team_name,
        resend=resend,
        locale=locale,
        public_entry_id=public_entry_id,
    )
    await enqueue_rendered_identity_email(store, rendered, to_email=to_email, purpose=purpose)
    return rendered


async def enqueue_rendered_identity_email(store: UserStore, rendered: RenderedIdentityEmail, *, to_email: str, purpose: str) -> None:
    """Persist an already materialized snapshot without querying mutable metadata."""
    team_id = rendered.snapshot.team_id
    await store.enqueue_smtp(
        to_email=to_email,
        subject=rendered.subject,
        body=rendered.text_body,
        html_body=rendered.html_body,
        purpose=purpose,
        team_id=team_id,
        public_entry_id=rendered.snapshot.public_entry_id,
        brand_revision_id=rendered.snapshot.brand_revision_id,
        branding_snapshot=rendered.snapshot.to_json(),
    )


async def _resolve_snapshot(
    request: Request,
    team_id: str,
    *,
    locale: str,
    public_entry_id: str | None = None,
) -> IdentityEmailBrandSnapshot:
    if locale not in {"zh-CN", "en-US"}:
        raise ValueError("identity email locale is invalid")
    from bowhead_gateway.public_url import team_public_access_context

    context = await team_public_access_context(request, team_id=team_id, entry_id=public_entry_id)
    if context.entry_id is None:
        return IdentityEmailBrandSnapshot(
            team_id=team_id,
            public_entry_id=None,
            brand_revision_id=None,
            brand_revision_checksum=None,
            application_name=product_name(),
            logo_url=None,
            public_base_url=context.base_url,
            accent=_DEFAULT_ACCENT,
            accent_ink=_DEFAULT_ACCENT_INK,
            recipe_version="bowhead-identity-email-v1",
            locale=locale,
        )

    trusted_context = context
    try:
        resolved = await resolve_public_branding_bundle(request.app, trusted_context)
    except Exception:
        logging.getLogger(__name__).exception("identity_email.brand_metadata_unavailable team_id=%s", team_id)
        resolved = None
    bundle = resolved.payload if resolved is not None else None
    brand = bundle.get("brand") if bundle else None
    team = bundle.get("team") if bundle else None
    effective = bundle.get("effective") if bundle else None
    scheme = effective.get("colorScheme") if isinstance(effective, dict) else None
    light = scheme.get("light") if isinstance(scheme, dict) else None
    application_name = brand.get("applicationName") if isinstance(brand, dict) and isinstance(brand.get("applicationName"), str) else product_name()
    logo = brand.get("logoOnLight") if isinstance(brand, dict) else None
    logo_url = None
    if isinstance(logo, dict) and logo.get("kind") == "asset" and isinstance(logo.get("path"), str):
        logo_url = _entry_url(context.base_url, logo["path"])
    revision_id = team.get("revisionId") if isinstance(team, dict) and isinstance(team.get("revisionId"), str) else None
    revision_checksum = getattr(resolved, "revision_checksum", None)
    accent = _safe_color(light.get("accent") if isinstance(light, dict) else None, _DEFAULT_ACCENT)
    accent_ink = _safe_color(light.get("accentInk") if isinstance(light, dict) else None, _DEFAULT_ACCENT_INK)
    return IdentityEmailBrandSnapshot(
        team_id=team_id,
        public_entry_id=context.entry_id,
        brand_revision_id=revision_id,
        brand_revision_checksum=(revision_checksum if isinstance(revision_checksum, str) else None),
        application_name=application_name,
        logo_url=logo_url,
        public_base_url=context.base_url,
        accent=accent,
        accent_ink=accent_ink,
        recipe_version="bowhead-identity-email-v1",
        locale=locale,
    )


def _safe_color(value: Any, fallback: str) -> str:
    if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
        return fallback
    try:
        int(value[1:], 16)
    except ValueError:
        return fallback
    return value.upper()


def _copy(
    kind: IdentityEmailKind,
    *,
    application_name: str,
    inviter_name: str | None,
    team_name: str | None,
    resend: bool,
    locale: str,
) -> tuple[str, str, str, str, str]:
    if locale == "en-US":
        if kind == "verify":
            return (
                f"Verify your {application_name} account",
                f"Welcome to {application_name}!",
                "Verify email",
                "This link expires in 7 days.",
                "If you did not request this, you can ignore this email.",
            )
        if kind in {"team_invite", "workspace_invite"}:
            target = f"the {team_name} team" if kind == "team_invite" and team_name else "a Space"
            invitation = f"{inviter_name} invited you" if inviter_name else "You have been invited"
            return (
                f"{'Invitation reminder' if resend else 'Invitation'} — {application_name}",
                f"{invitation} to join {target} in {application_name}.",
                "View invitation",
                "This link expires in 7 days.",
                "Your administrator controls team and data access.",
            )
        return (
            f"Reset your {application_name} password",
            f"A password reset was requested for your {application_name} account.",
            "Reset password",
            "This link expires in 24 hours.",
            "If you did not request this, ignore this email. Your password will stay the same.",
        )
    if kind == "verify":
        return (
            f"验证你的 {application_name} 账号",
            f"欢迎注册 {application_name}！",
            "验证邮箱",
            "链接 7 天有效。",
            "如果不是你的操作，请忽略。",
        )
    if kind in {"team_invite", "workspace_invite"}:
        target = f"{team_name} 的团队" if kind == "team_invite" and team_name else "workspace"
        prefix = f"{inviter_name} 邀请你" if inviter_name else "你被邀请"
        return (
            f"{'邀请提醒' if resend else '邀请加入'} — {application_name}",
            f"{prefix}加入 {application_name} {target}。",
            "查看邀请",
            "链接 7 天有效。",
            "团队及数据权限仍由管理员控制。",
        )
    return (
        f"重置 {application_name} 密码",
        f"你（或冒名者）申请重置 {application_name} 密码。",
        "重置密码",
        "链接 24 小时有效。",
        "如果不是你，忽略此邮件，密码不会改变。",
    )


def _entry_url(base_url: str, path: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("identity email action path must be root-relative")
    return f"{base_url}{quote(path, safe='/%?=&:._~-')}"


__all__ = [
    "IdentityEmailBrandSnapshot",
    "RenderedIdentityEmail",
    "enqueue_identity_email",
    "render_identity_email",
]

"""P4.5.1 — Auth routes (register / login / logout / me).

JWT cookie httpOnly secure. 全 JWT 单轨 (用户拍 12.7).
- register: email + password (argon2 不装, 用 scrypt) → user
- login: email + password verify → JWT cookie
- logout: clear cookie
- me: return current user from JWT

In-memory IP 限流 5/min for register/login (12.13).

P0 没 email verify (P4.5.9) / OAuth (P4.5.4) / datarc pass (P4.5.3) — 这里只 email/password.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from dataclasses import replace
from typing import Any, Final
from urllib.parse import quote

import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from bowhead_gateway.product_identity import product_copy, product_name

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


# Sprint α G6 (r4): domain white-list — MEMBER tier (NOT admin per Sec P0-S-7)
# Old env BOWHEAD_AUTO_ADMIN_DOMAINS is deprecated; new env BOWHEAD_AUTO_MEMBER_DOMAINS.
# Both supported for 1-sprint backward compat; deprecation warned once at startup.

_AUTO_MEMBER_DOMAINS_CACHE: list[str] | None = None
_DEPRECATION_WARNED = False


def _auto_member_domains() -> list[str]:
    """env BOWHEAD_AUTO_MEMBER_DOMAINS=datarc.cn,partner.com → 全小写 list.

    Backward compat: falls back to old BOWHEAD_AUTO_ADMIN_DOMAINS with deprecation
    warning logged once per process. Cached after first read.
    """
    global _AUTO_MEMBER_DOMAINS_CACHE, _DEPRECATION_WARNED
    if _AUTO_MEMBER_DOMAINS_CACHE is not None:
        return _AUTO_MEMBER_DOMAINS_CACHE
    raw_new = os.environ.get("BOWHEAD_AUTO_MEMBER_DOMAINS") or ""
    raw_old = os.environ.get("BOWHEAD_AUTO_ADMIN_DOMAINS") or ""
    if raw_new:
        domains = [d.strip().lower() for d in raw_new.split(",") if d.strip()]
    elif raw_old:
        if not _DEPRECATION_WARNED:
            log.warning(
                "BOWHEAD_AUTO_ADMIN_DOMAINS is deprecated (sprint α). "
                "Rename to BOWHEAD_AUTO_MEMBER_DOMAINS — same value semantics, "
                "but users no longer auto-become admin (member tier only).",
            )
            _DEPRECATION_WARNED = True
        domains = [d.strip().lower() for d in raw_old.split(",") if d.strip()]
    else:
        domains = []
    _AUTO_MEMBER_DOMAINS_CACHE = domains
    return domains


def is_auto_member_email(email: str) -> bool:
    """email 后缀匹配 white-list → True (member tier + skip email verify)."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1].lower().strip()
    return any(domain == d or domain.endswith("." + d) for d in _auto_member_domains())


# Backward-compat alias (1 sprint — sprint β deletes); callers should migrate.
is_auto_admin_email = is_auto_member_email  # DEPRECATED — renamed sprint α
_auto_admin_domains = _auto_member_domains  # DEPRECATED — renamed sprint α

# P4.5.21: password complexity (lightweight, no zxcvbn).
# Rules: 8-200 characters + at least 1 letter + at least 1 digit. Symbols optional (friendly UX).
_PWD_LETTER_RE = re.compile(r"[A-Za-z]")
_PWD_DIGIT_RE = re.compile(r"\d")


def _validate_password_strength(pwd: str) -> None:
    """校验通过返 None, 否则 raise HTTPException(422)."""
    if len(pwd) < 8 or len(pwd) > 200:
        raise HTTPException(422, {"code": "password_length_invalid"})
    if not _PWD_LETTER_RE.search(pwd):
        raise HTTPException(422, {"code": "password_letter_required"})
    if not _PWD_DIGIT_RE.search(pwd):
        raise HTTPException(422, {"code": "password_digit_required"})
    # Blacklist top-N weak passwords.
    if pwd.lower() in {"password", "password1", "12345678", "qwerty12", "abcd1234", "11111111", "iloveyou", "admin123"}:
        raise HTTPException(422, {"code": "password_too_common"})


import contextlib
from datetime import UTC

from bowhead_core.access_control import normalize_workspace_role, workspace_role_rank
from bowhead_core.credit_activation import credit_capabilities_for_role
from bowhead_core.entitlements import EntitlementDenied
from bowhead_core.kb.generations import use_guard_enabled
from bowhead_core.language_context import german_ai_output_enabled
from bowhead_core.space_governance import (
    SpaceGovernanceError,
    space_admin_v2_enabled,
)
from bowhead_core.user_cron import next_monthly_credit_reset
from bowhead_core.user_store import _UNSET, User, UserStore, _now

from bowhead_gateway._entitlement_gate import require_not_readonly, seat_denied_to_http
from bowhead_gateway.routes.planning_flags import planning_enabled as _planning_enabled

router = APIRouter()


@router.get("/login-capabilities")
async def login_capabilities(request: Request) -> dict[str, object]:
    """Public, credential-free projection of sign-in methods usable right now."""
    from bowhead_gateway.auth_policy import configured_oauth_providers
    from bowhead_gateway.edition_identity import identity_adapter

    adapter = identity_adapter(request.app)
    oidc_available = await adapter.login_available(request) if adapter is not None else False
    return {
        "oauth_providers": list(configured_oauth_providers()),
        "enterprise_oidc": oidc_available,
    }


# ─── helpers ────────────────────────────────────────────


def _agent_eval_alignment_feature_on() -> bool:
    from bowhead_core.agent_eval_alignment import alignment_enabled

    return alignment_enabled()


def _shopify_plugin_feature_on() -> bool:
    from bowhead_core.builtin_shopify_tool import feature_enabled

    return feature_enabled()


def _google_workspace_feature_on() -> bool:
    from bowhead_core.google_workspace import feature_enabled

    return feature_enabled()


async def _google_workspace_available(request: Request, team_id: str) -> bool:
    """True when Google Workspace is on and the team connection is enabled."""
    from bowhead_core.google_workspace import PROVIDER, feature_enabled

    store = getattr(request.app.state, "user_store", None)
    if not feature_enabled() or store is None or not team_id:
        return False
    try:
        connection = await store.get_team_builtin_tool(team_id, PROVIDER)
    except Exception:
        log.warning("me.google_workspace_capability_failed team=%s", team_id, exc_info=True)
        return False
    return connection is not None and connection.status == "enabled"


def _store(request: Request) -> UserStore:
    s: UserStore | None = getattr(request.app.state, "user_store", None)
    if s is None:
        raise HTTPException(503, "user_store not configured")
    return s


def _jwt_secret_keys() -> list[tuple[str, str]]:
    """P4.5.19: multi-key support for JWT rotation.

    Active key (first) signs new tokens. All keys verify (try in order).
    Env: BOWHEAD_JWT_SECRETS = "kid1=key1,kid2=key2" (kid 标识 + 顺序定 active).
    Fallback: BOWHEAD_JWT_SECRET single key (kid="default").
    返 [(kid, key), ...] active first.
    """
    multi = os.environ.get("BOWHEAD_JWT_SECRETS")
    if multi:
        out: list[tuple[str, str]] = []
        for chunk in multi.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" in chunk:
                kid, key = chunk.split("=", 1)
                out.append((kid.strip(), key.strip()))
            else:
                out.append((f"k{len(out)}", chunk))
        if out:
            return out
    s = os.environ.get("BOWHEAD_JWT_SECRET")
    if s:
        return [("default", s)]
    return []


def derive_jwt_secret_keys(purpose: str) -> list[tuple[str, str]]:
    """Derive purpose-scoped secrets from the configured JWT signing keyring."""
    domain = f"bowhead/{purpose}/v1".encode()
    return [(key_id, hmac.new(secret.encode("utf-8"), domain, hashlib.sha256).hexdigest()) for key_id, secret in _jwt_secret_keys()]


def _email_verify_secret_keys() -> list[str]:
    keys = [secret for _key_id, secret in derive_jwt_secret_keys("email-verify")]
    if not keys:
        raise HTTPException(500, "BOWHEAD_JWT_SECRET(S) not set")
    return keys


def _jwt_secret(request: Request) -> str:
    """Active (first) key for signing. 旧调用方仍可拿单 key 用."""
    keys = _jwt_secret_keys()
    if not keys:
        raise HTTPException(500, "BOWHEAD_JWT_SECRET(S) not set")
    return keys[0][1]


def _safe_next_path(value: str | None) -> str | None:
    """Allow only same-origin app paths for post-auth navigation."""
    if not value:
        return None
    path = value.strip()
    if len(path) > 2048:
        return None
    if any(ord(ch) < 32 for ch in path):
        return None
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return None
    return path


def _cookie_samesite() -> str:
    """P4.5.22: BOWHEAD_COOKIE_SAMESITE env (lax|none|strict). 默认 lax."""
    v = (os.environ.get("BOWHEAD_COOKIE_SAMESITE") or "lax").lower().strip()
    if v not in ("lax", "none", "strict"):
        v = "lax"
    return v


def _public_entry_is_https() -> bool:
    """当前请求解析出的**浏览器可见**入口是不是 https。

    注意不能看 `request.url.scheme`：TLS 常在客户侧代理终结，网关自己看到的是
    http。要看的是公开入口登记的 base_url。
    """
    from bowhead_gateway.public_entry_context import current_request_public_access_context

    context = current_request_public_access_context()
    return bool(context is not None and context.origin.startswith("https://"))


def _cookie_secure() -> bool:
    """Require Secure for SameSite=None and browser-visible HTTPS entries.

    Use the same public-entry origin as other authentication cookies. The
    production environment flag remains a fallback when no HTTPS entry is known.
    """
    if _cookie_samesite() == "none":
        return True
    from bowhead_gateway.public_entry_context import current_request_public_access_context

    address = current_request_public_access_context()
    if address is not None and (address.prefix_known or address.match_source == "forwarded_url"):
        return address.origin.startswith("https://")
    return os.environ.get("BOWHEAD_PROD") == "1"


# Supported UI locales only; unknown values count as unset, without guessing for users.
_UI_LOCALES: Final = ("zh-CN", "en-US", "de-DE", "fr-FR")
_LOCALE_COOKIE = "NEXT_LOCALE"
_LOCALE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # Same lifetime as frontend LocaleSwitcher.


async def _sync_locale_cookie(request: Request, response: Response, user_id: str) -> None:
    """把账号里存着的 UI 语言同步到 `NEXT_LOCALE` cookie。

    ★ 为什么需要这一步：语言偏好**写了三处、读零处**。切换器把 `ui_language` 写给服务端
    （另外还有 `users.locale`、`output_language`），但决定 UI 语言的两条读路径 ——
    网关选哪份静态构建（`static_localized._detect_locale`）与前端
    `ClientLocaleProvider.detectLocale()` —— 都只看 cookie，cookie 没有就跟浏览器语言走。
    于是换设备、清 cookie、无痕窗口时，设过的语言等于没设。2026-08-19 在 116 实测到：
    账号三处全是 `zh-CN`，页面却是英文，顶栏还宣称 current: English。

    这里不新增第三条判据，而是让**存下来的设置成为 cookie 的来源**：cookie 仍是唯一传输
    载体，两条读路径都不用改。挂在登录（新设备第一次就对）与 `/me`（修既有会话）上。

    非 httpOnly：前端 `detectLocale()` 要用 `document.cookie` 读它。它只是一个展示偏好，
    不携带任何凭据。
    """

    store = getattr(request.app.state, "user_settings_store", None)
    if store is None:
        return
    try:
        stored = await store.get(user_id, "ui_language")
    except Exception as error:  # Failed language-preference reads must never break login.
        log.warning("locale_cookie.read_failed user=%s err=%s", user_id, error)
        return
    if stored == "auto":
        if request.cookies.get(_LOCALE_COOKIE):
            response.delete_cookie(_LOCALE_COOKIE, path="/", secure=_cookie_secure(), httponly=False, samesite=_cookie_samesite())
        return
    if stored not in _UI_LOCALES:
        return
    if request.cookies.get(_LOCALE_COOKIE) == stored:
        return  # Already matches; avoid Set-Cookie on every /me call.
    response.set_cookie(
        key=_LOCALE_COOKIE,
        value=stored,
        httponly=False,
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),  # type: ignore[arg-type]
        max_age=_LOCALE_COOKIE_MAX_AGE,
        path="/",
    )


def _set_jwt_cookie(response: Response, token: str, max_age: int = 3600) -> None:
    """P4.5.22: 统一 access cookie 入口, 全 auth/oauth 路径走这里."""
    response.set_cookie(
        key="bowhead_jwt",
        value=token,
        httponly=True,
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),  # type: ignore[arg-type]
        max_age=max_age,
    )


# P4.5.27: refresh token (30d), dual cookies with access; rotate at /refresh.
_REFRESH_COOKIE = "bowhead_refresh"
_REFRESH_TTL_SEC = 30 * 24 * 3600  # 30d
_REFRESH_V1_PREFIX = "v1"
_AUTH_SESSION_GENERATION_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_DEV_AUTH_SESSION_GENERATION = secrets.token_urlsafe(16)


def _new_auth_session_generation() -> str:
    """Return a non-credential 128-bit fence that is stable for one login chain."""
    return secrets.token_urlsafe(16)


def _new_refresh_token(auth_session_generation: str) -> str:
    # The whole value remains an opaque credential and is hashed at rest. Embedding
    # the non-credential generation lets refresh rotation preserve the login chain.
    return f"{_REFRESH_V1_PREFIX}.{auth_session_generation}.{secrets.token_urlsafe(32)}"


def _refresh_auth_session_generation(raw_token: str) -> str | None:
    parts = raw_token.split(".", 2)
    if len(parts) == 3 and parts[0] == _REFRESH_V1_PREFIX and _AUTH_SESSION_GENERATION_RE.fullmatch(parts[1]):
        return parts[1]
    return None


_REFRESH_COOKIE_FALLBACK_PATH = "/"

# Tabs of one browser share the refresh cookie and can send the same token at
# almost the same time. A request carrying a predecessor rotated within this
# window receives a non-revoking 401 that leaves the shared cookies intact; it
# never receives credentials. Later replays keep the normal rejection.
_REFRESH_ROTATION_GRACE_SEC = 30.0
REFRESH_SUPERSEDED_CODE = "refresh_superseded"


def _refresh_superseded_error() -> HTTPException:
    return HTTPException(
        401,
        {
            "code": REFRESH_SUPERSEDED_CODE,
            "message": ("This sign-in was renewed by another tab moments ago. Retry the request; the browser already holds the renewed session."),
        },
    )


def _refresh_cookie_path() -> str:
    """Scope refresh cookies to the trusted public-entry prefix.

    A known prefix uses its authentication path. A matched root entry uses
    /api/v1/auth. When proxy handling leaves the prefix unknown, use / so the
    browser can still send the refresh cookie to a prefixed authentication URL.
    """
    from bowhead_gateway.public_entry_context import current_request_public_access_context
    from bowhead_gateway.public_url import public_app_path

    context = current_request_public_access_context()
    if context is None:
        return _REFRESH_COOKIE_FALLBACK_PATH
    if context.prefix_known:
        return public_app_path("/api/v1/auth")
    return _REFRESH_COOKIE_FALLBACK_PATH


def _refresh_cookie_paths() -> tuple[str, ...]:
    """本部署下 refresh cookie 可能落过的所有 Path（去重，保持顺序）。

    清理时必须全清：同一个浏览器可能先在无入口上下文下拿到 `/` 的 cookie、
    之后又在有入口的请求里拿到窄 Path 的，两份都会被回传，服务端 `cookies.get`
    只取其一，另一份就成了幽灵。
    """
    from bowhead_gateway.public_url import public_app_path

    seen: list[str] = []
    for candidate in (_refresh_cookie_path(), public_app_path("/api/v1/auth"), _REFRESH_COOKIE_FALLBACK_PATH):
        if candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


def _set_refresh_cookie(response: Response, raw_token: str) -> None:
    """同 _set_jwt_cookie 的 same-site/secure 策略, max_age 30d."""
    path = _refresh_cookie_path()
    # Clear same-name cookies at other Paths first, avoiding old/new coexistence (see _refresh_cookie_paths).
    for stale in _refresh_cookie_paths():
        if stale != path:
            response.delete_cookie(key=_REFRESH_COOKIE, path=stale)
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=raw_token,
        httponly=True,
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),  # type: ignore[arg-type]
        max_age=_REFRESH_TTL_SEC,
        path=path,
    )


def _clear_refresh_cookie(response: Response) -> None:
    for path in _refresh_cookie_paths():
        response.delete_cookie(key=_REFRESH_COOKIE, path=path)


async def _issue_token_pair(
    user: User,
    request: Request,
    response: Response,
    store: UserStore,
) -> tuple[str, str]:
    """同时签 access (1h JWT) + refresh (30d opaque) 并下两个 cookie. 返 (access, refresh)."""
    from datetime import datetime, timedelta

    auth_session_generation = _new_auth_session_generation()
    await _register_composer_auth_generation(
        request,
        user_id=user.id,
        generation=auth_session_generation,
        issued_at_epoch=time.time(),
    )
    access = _issue_jwt(
        user,
        _jwt_secret(request),
        auth_session_generation=auth_session_generation,
    )
    refresh_raw = _new_refresh_token(auth_session_generation)
    expires_at = (datetime.now(UTC) + timedelta(seconds=_REFRESH_TTL_SEC)).isoformat()
    await store.record_refresh_token(
        user_id=user.id,
        raw_token=refresh_raw,
        expires_at=expires_at,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    _set_jwt_cookie(response, access)
    _set_refresh_cookie(response, refresh_raw)
    return access, refresh_raw


def _issue_jwt(
    user: User,
    secret: str,
    ttl_sec: int = 3600,
    *,
    auth_session_generation: str | None = None,
    auth_time: int | None = None,
) -> str:
    """access token 1h (12.6). P4.5.19: header 加 kid 标记签名 key.

    Sprint β G10 P0-S-3: include `pwd_v` claim — invalidates stolen-cookie access
    tokens after password change.
    """
    now = int(time.time())
    payload = {
        "sub": user.id,
        "team_id": user.team_id,
        "email": user.email,
        "tier": user.tier,
        "pwd_v": getattr(user, "pwd_version", 0),
        "asg": auth_session_generation or _new_auth_session_generation(),
        "iat": now,
        "auth_time": now if auth_time is None else auth_time,
        "exp": now + ttl_sec,
    }
    # Find kid: first key matching secret (active key by default).
    kid = "default"
    for k, v in _jwt_secret_keys():
        if v == secret:
            kid = k
            break
    return jwt.encode(payload, secret, algorithm="HS256", headers={"kid": kid})


def _issue_invite_jwt(payload: dict[str, Any]) -> str:
    keys = _jwt_secret_keys()
    if not keys:
        raise HTTPException(500, "BOWHEAD_JWT_SECRET(S) not set")
    kid, secret = keys[0]
    return jwt.encode(payload, secret, algorithm="HS256", headers={"kid": kid})


def _decode_jwt(token: str, secret: str) -> dict | None:
    """P4.5.19: 单 secret 解 (legacy)."""
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def _decode_jwt_multi(token: str) -> dict | None:
    """P4.5.19: multi-key verify. 试所有 key, 优先 token header 指定的 kid.

    token revocation: 若需踢 user 全部 token, 直接从 BOWHEAD_JWT_SECRETS 移除该 kid.
    """
    keys = _jwt_secret_keys()
    if not keys:
        return None
    # peek header for kid
    try:
        hdr = jwt.get_unverified_header(token)
        kid = hdr.get("kid")
    except jwt.PyJWTError:
        kid = None
    if kid:
        for k, v in keys:
            if k == kid:
                p = _decode_jwt(token, v)
                if p:
                    return p
                # This kid's key exists but signature mismatches (tampered/expired): no fall-through, preventing bypass.
                return None
    # No kid or absent from keyset: try all keys (legacy compatibility).
    for _, v in keys:
        p = _decode_jwt(token, v)
        if p:
            return p
    return None


def _decode_invite_jwt(token: str) -> tuple[str, dict[str, Any] | None]:
    """Decode invite JWT and distinguish expired from invalid for preview UX."""
    keys = _jwt_secret_keys()
    if not keys:
        return ("invalid", None)
    try:
        hdr = jwt.get_unverified_header(token)
        kid = hdr.get("kid")
    except jwt.PyJWTError:
        kid = None

    ordered = keys
    if kid:
        ordered = [(k, v) for k, v in keys if k == kid]
        if not ordered:
            return ("invalid", None)

    saw_expired = False
    for _kid, secret in ordered:
        try:
            return ("ok", jwt.decode(token, secret, algorithms=["HS256"]))
        except jwt.ExpiredSignatureError:
            saw_expired = True
            if kid:
                return ("expired", None)
        except jwt.PyJWTError:
            if kid:
                return ("invalid", None)
            continue
    return ("expired" if saw_expired else "invalid", None)


def _invite_row_status(row: dict[str, Any] | None) -> str:
    if not row:
        return "invalid"
    status_value = str(row.get("status") or "")
    if status_value == "revoked":
        return "revoked"
    if status_value == "consumed":
        return "consumed"
    if status_value != "active":
        return "invalid"
    if str(row.get("expires_at") or "") < _now():
        return "expired"
    return "valid"


def _payload_invite_type(payload: dict[str, Any]) -> str | None:
    scope = payload.get("scope")
    if scope == "team":
        return "team"
    if scope == "workspace" or payload.get("workspace_id"):
        return "workspace"
    return None


def _row_matches_payload(
    invite_type: str,
    row: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    invited_email = str(payload.get("invited_email") or "").lower().strip()
    if invite_type == "team":
        return (
            str(row.get("team_id") or "") == str(payload.get("team_id") or "")
            and str(row.get("invited_email") or "").lower().strip() == invited_email
            and str(row.get("team_role") or "member") == str(payload.get("team_role") or "member")
        )
    return (
        str(row.get("workspace_id") or "") == str(payload.get("workspace_id") or "")
        and str(row.get("invited_email") or "").lower().strip() == invited_email
        and str(row.get("role") or "member") == str(payload.get("role") or "member")
    )


async def _resolve_invite_row(
    store: UserStore,
    token: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    invite_type = _payload_invite_type(payload)
    if invite_type is None:
        return None

    invite_id = str(payload.get("invite_id") or "")
    if invite_id:
        row = await store.get_team_invite_by_id(invite_id) if invite_type == "team" else await store.get_workspace_invite_by_id(invite_id)
        if row and _row_matches_payload(invite_type, row, payload):
            return {"invite_type": invite_type, "row": row, "resolution": "exact_id"}
        return None

    row = await store.get_team_invite_by_token_hash(token) if invite_type == "team" else await store.get_workspace_invite_by_token_hash(token)
    if row and _row_matches_payload(invite_type, row, payload):
        return {"invite_type": invite_type, "row": row, "resolution": "exact_hash"}

    # Legacy compatibility: old links without signed invite_id may survive resend if
    # there is still an active row for the signed scope/email target. This returns only
    # an active-valid row; it does not infer revoked/consumed.
    invited_email = str(payload.get("invited_email") or "").lower().strip()
    if invite_type == "team":
        legacy = await store.get_active_team_invite_by_email(
            str(payload.get("team_id") or ""),
            invited_email,
        )
    else:
        legacy = await store.get_active_workspace_invite_by_email(
            str(payload.get("workspace_id") or ""),
            invited_email,
        )
    if legacy and _row_matches_payload(invite_type, legacy, payload):
        return {"invite_type": invite_type, "row": legacy, "resolution": "legacy_fallback"}
    return None


def _invite_ready_route(
    invite_type: str,
    *,
    has_workspace_access: bool = False,
) -> str:
    if invite_type == "workspace" or has_workspace_access:
        return "/invite/ready?state=workspace_ready"
    return "/invite/ready?state=wait_for_workspace"


def _invite_next_route(
    invite_type: str,
    requires_verification: bool,
    *,
    has_workspace_access: bool = False,
) -> tuple[str, str]:
    if requires_verification:
        ready_route = _invite_ready_route(
            invite_type,
            has_workspace_access=has_workspace_access,
        )
        return ("verify_email", f"/invite/verify-pending?next={quote(ready_route, safe='')}")
    ready_route = _invite_ready_route(
        invite_type,
        has_workspace_access=has_workspace_access,
    )
    profile_route = f"/onboarding/profile?source=invite&next={quote(ready_route, safe='')}"
    return ("profile_setup", profile_route)


def _build_invite_result(
    *,
    invite_type: str,
    row: dict[str, Any],
    requires_verification: bool,
    workspace_name: str | None = None,
    team_id: str | None = None,
    team_name: str | None = None,
    accept_status: str | None = None,
    has_workspace_access: bool = False,
) -> dict[str, Any]:
    next_step, next_route = _invite_next_route(
        invite_type,
        requires_verification,
        has_workspace_access=has_workspace_access,
    )
    result: dict[str, Any] = {
        "invite_type": invite_type,
        "status": "verification_required" if requires_verification else (accept_status or "accepted"),
        "next_step": next_step,
        "next_route": next_route,
        "ready_route": _invite_ready_route(
            invite_type,
            has_workspace_access=has_workspace_access,
        ),
        "team": {
            "id": team_id or row.get("team_id"),
            "name": team_name,
        },
    }
    if invite_type == "workspace":
        result["workspace"] = {
            "id": row.get("workspace_id"),
            "role": row.get("role") or "member",
        }
        if workspace_name:
            result["workspace"]["name"] = workspace_name
    return result


def _extract_jwt_payload(request: Request) -> dict | None:
    """JWT cookie/Bearer → decoded payload (or None)."""
    token: str | None = None
    cookie = request.cookies.get("bowhead_jwt")
    if cookie:
        token = cookie
    elif (auth := request.headers.get("Authorization")) and auth.startswith("Bearer "):
        token = auth[7:]
    if not token:
        return None
    return _decode_jwt_multi(token)


def _auth_session_generation(request: Request) -> str:
    """Project a non-credential stale-response fence for ``/auth/me``.

    New tokens carry ``asg``. A legacy token receives a deterministic bridge for
    the lifetime of that access token; explicit dev fallback receives a
    process-local generation.
    """
    payload = _extract_jwt_payload(request) or {}
    generation = payload.get("asg")
    if isinstance(generation, str) and _AUTH_SESSION_GENERATION_RE.fullmatch(generation):
        return generation

    token = request.cookies.get("bowhead_jwt")
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    if token:
        digest = hashlib.sha256(f"bowhead-auth-session-legacy.v1\0{token}".encode()).hexdigest()
        return f"legacy_{digest[:32]}"
    return f"dev_{_DEV_AUTH_SESSION_GENERATION}"


def _auth_session_generation_hash(generation: str) -> str:
    """One-way durable identifier for a login chain; never persist raw ``asg``."""
    return hashlib.sha256(
        b"bowhead/chat-auth-session-generation/v1\0" + generation.encode(),
    ).hexdigest()


async def _invalidate_composer_auth_generation(
    request: Request,
    *,
    user_id: str,
    generation: str,
) -> None:
    from bowhead_core.composer_rollout import composer_submission_protocol_enabled

    protocol_enabled = composer_submission_protocol_enabled()
    store = getattr(request.app.state, "session_store", None)
    invalidate = getattr(store, "invalidate_chat_auth_session_generation", None)
    if not callable(invalidate):
        if not protocol_enabled:
            return
        raise HTTPException(
            503,
            {"error": {"code": "auth_session_state_unavailable"}},
        )
    try:
        await invalidate(
            user_id=user_id,
            auth_session_generation_hash=_auth_session_generation_hash(generation),
        )
    except Exception as exc:
        if not protocol_enabled:
            log.warning("auth.composer_generation_invalidation_unavailable")
            return
        raise HTTPException(
            503,
            {"error": {"code": "auth_session_state_unavailable"}},
        ) from exc


async def _register_composer_auth_generation(
    request: Request,
    *,
    user_id: str,
    generation: str,
    issued_at_epoch: float | None = None,
) -> None:
    from bowhead_core.composer_rollout import composer_submission_protocol_enabled

    protocol_enabled = composer_submission_protocol_enabled()
    store = getattr(request.app.state, "session_store", None)
    register = getattr(store, "register_chat_auth_session_generation", None)
    if not callable(register):
        if not protocol_enabled:
            return
        raise HTTPException(
            503,
            {"error": {"code": "auth_session_state_unavailable"}},
        )
    try:
        if issued_at_epoch is None:
            raw_iat = (_extract_jwt_payload(request) or {}).get("iat")
            issued_at_epoch = float(raw_iat) if isinstance(raw_iat, (int, float)) else None
        active = await register(
            user_id=user_id,
            auth_session_generation_hash=_auth_session_generation_hash(generation),
            issued_at_epoch=issued_at_epoch,
        )
    except Exception as exc:
        if not protocol_enabled:
            log.warning("auth.composer_generation_registration_unavailable")
            return
        raise HTTPException(
            503,
            {"error": {"code": "auth_session_state_unavailable"}},
        ) from exc
    if not active:
        raise HTTPException(
            401,
            {"error": {"code": "auth_session_generation_inactive"}},
        )


async def _invalidate_all_composer_auth_generations(
    request: Request,
    *,
    user_id: str,
) -> None:
    from bowhead_core.composer_rollout import composer_submission_protocol_enabled

    protocol_enabled = composer_submission_protocol_enabled()
    store = getattr(request.app.state, "session_store", None)
    invalidate_all = getattr(store, "invalidate_all_chat_auth_session_generations", None)
    if not callable(invalidate_all):
        if not protocol_enabled:
            return
        raise HTTPException(
            503,
            {"error": {"code": "auth_session_state_unavailable"}},
        )
    try:
        await invalidate_all(user_id=user_id)
    except Exception as exc:
        if not protocol_enabled:
            log.warning("auth.composer_generations_invalidation_unavailable")
            return
        raise HTTPException(
            503,
            {"error": {"code": "auth_session_state_unavailable"}},
        ) from exc


def _capability_metadata(
    request: Request,
    *,
    team_id: str,
    capabilities: dict[str, Any],
) -> dict[str, str]:
    from datetime import datetime

    projection = json.dumps(
        {"team_id": team_id, "capabilities": capabilities},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    revision = hashlib.sha256(projection.encode()).hexdigest()[:24]
    return {
        "capability_revision": f"cap_v1_{revision}",
        "fetched_at": datetime.now(UTC).isoformat(),
        "auth_session_generation": _auth_session_generation(request),
    }


def _dev_user_fallback_enabled(request: Request) -> bool:
    """True only for the explicit local/test auth bypass.

    This mirrors ``_current_user``. Ambient ``auth_mode=dev`` is not enough:
    callers must opt in with ``BOWHEAD_DEV_USER_FALLBACK=1`` so SaaS/prod
    deployments cannot silently assume ``dev_default_user``.
    """
    settings = getattr(request.app.state, "settings", None)
    auth_mode = getattr(settings, "auth_mode", os.environ.get("BOWHEAD_AUTH_MODE", "prod"))
    return auth_mode == "dev" and os.environ.get("BOWHEAD_DEV_USER_FALLBACK", "0") == "1"


def signed_in_user_id_or_none(request: Request) -> str | None:
    """Best-effort caller id for non-authorizing side reads (error-message localization).

    Same token extraction and signature verification as ``_authenticated_identity``,
    minus every branch that *rejects* a caller: no user-store lookup, no status /
    ``pwd_v`` / email-verification checks, and it never raises. Anything that authorizes
    MUST keep using ``_current_user``; this only answers "whose UI preference should
    this response be rendered in", and its caller is the error handler — raising there
    would replace the user's real error with a 500.
    """
    try:
        token = request.cookies.get("bowhead_jwt")
        if not token:
            header = request.headers.get("Authorization")
            if header and header.startswith("Bearer "):
                token = header[7:]
        if not token:
            if not _dev_user_fallback_enabled(request):
                return None
            hdr = request.headers.get("x-bowhead-user-id")
            if hdr and hdr.strip():
                return hdr.strip()
            settings = getattr(request.app.state, "settings", None)
            return getattr(settings, "dev_default_user", None) or None
        payload = _decode_jwt_multi(token)
        subject = payload.get("sub") if payload else None
        return subject if isinstance(subject, str) and subject else None
    except Exception:
        # Localization must never be able to break the response it is localizing.
        return None


def _is_dev_auth_mode(settings) -> bool:
    """``auth_mode == 'dev'`` predicate for the WebSocket/turn helpers that have no
    ``Request`` (``resolve_ws_user_id`` / ``assert_turn_access``).

    Any non-dev deployment (116 SaaS auth_mode='prod', licensed local, oidc/jwt) MUST
    require a verified JWT. The HTTP sync resolvers below use the STRICTER
    ``_dev_user_fallback_enabled`` (which also requires ``BOWHEAD_DEV_USER_FALLBACK=1``);
    both are fail-closed on non-dev. Missing attr → non-dev (fail-closed).
    """
    return getattr(settings, "auth_mode", "prod") == "dev"


def resolve_user_id_sync(request: Request) -> str:
    """Resolve a user id for legacy sync routes.

    Security boundary:
    - JWT/Bearer works in every mode.
    - ``X-Bowhead-User-Id`` and ``dev_default_user`` are accepted only behind
      the explicit dev fallback gate used by ``_current_user``.
    - SaaS/prod without a token is 401, never an implicit dev user.
    """
    payload = _extract_jwt_payload(request)
    if payload and payload.get("sub"):
        return str(payload["sub"])
    if _dev_user_fallback_enabled(request):
        hdr = request.headers.get("x-bowhead-user-id")
        if hdr and hdr.strip():
            return hdr.strip()
        settings = request.app.state.settings
        return settings.dev_default_user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "auth required")


async def resolve_user_or_stub(request: Request) -> tuple[str, str | None]:
    """Resolve ``(user_id, team_id)`` for chat's legacy entrypoint.

    Missing JWT only returns a stub when the explicit dev fallback is enabled.
    """
    payload = _extract_jwt_payload(request)
    if payload and payload.get("sub"):
        return str(payload["sub"]), payload.get("team_id")
    if not _dev_user_fallback_enabled(request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "auth_required"})
    settings = request.app.state.settings
    return settings.dev_default_user, None


def resolve_ws_user_id(websocket) -> str | None:
    """WebSocket 版身份解析 (无 Request, 不抛 HTTPException). JWT cookie/Bearer → sub; 仅 dev
    auth_mode 退 dev_default_user; 非 dev 无 JWT → None (调用方负责 close). 镜像 resolve_user_id_sync
    的 fail-closed 语义 —— task_plan WS 等端点用 (原 accept 后零鉴权, 任何人知 session_id 即可连/读
    plan/注入 decision)."""
    token = websocket.cookies.get("bowhead_jwt")
    if not token:
        auth = websocket.headers.get("authorization")
        if auth and auth.startswith("Bearer "):
            token = auth[7:]
    if token:
        payload = _decode_jwt_multi(token)
        if payload and payload.get("sub"):
            return str(payload["sub"])
    settings = getattr(websocket.app.state, "settings", None)
    if _is_dev_auth_mode(settings):
        return getattr(settings, "dev_default_user", None)
    return None


async def assert_turn_access(request: Request, turn_id: str, session_store) -> str:
    """SSE turn 端点的 fail-closed 鉴权: ① resolve_user_id_sync (非 dev 匿名 → 401) ② turn→session→
    user_id owner 校验 (读别人的 turn → 403). 返回 caller user_id. 原 /chat/stream/{turn_id} 与
    /turns/{id}/replay 仅凭 turn_id 即可读他人 turn 事件 (IDOR). session_store None / turn 无 session
    记录 → 只保留 ① 的鉴权 (无 owner 可查)."""
    uid = resolve_user_id_sync(request)
    if session_store is not None:
        turn = await session_store.get_turn(turn_id)
        sid = getattr(turn, "session_id", None) if turn is not None else None
        if sid:
            session = await session_store.get_session(sid)
            if session is not None and session.user_id != uid:
                raise HTTPException(403, "not your turn")
    return uid


def resolve_kb_role(request: Request) -> str:
    """V7 kb-agentic-fix §2.6: 从 JWT email 解 user RBAC role.

    BOWHEAD_AUTO_ADMIN_DOMAINS env (per project_p45_prod_env.md): 邮箱域名匹配
    → team_admin (V1 mock); 真 auth ship 后切 JWT roles claim.

    chat.py 和 kb.py 共用这个 helper. JWT payload 没 email (老 JWT) → user.
    """
    payload = _extract_jwt_payload(request)
    if not payload or not payload.get("email"):
        return "user"
    email = str(payload.get("email", "")).strip().lower()
    if "@" not in email:
        return "user"
    domain = email.split("@", 1)[1]
    raw = os.environ.get("BOWHEAD_AUTO_ADMIN_DOMAINS", "")
    domains = {d.strip().lower() for d in raw.split(",") if d.strip()}
    if domain in domains:
        return "team_admin"
    return "user"


def assert_prod_auth_safe() -> None:
    """Sprint α G9 startup guard: prod mode must NOT have dev fallback enabled.

    Called by main.py at app start. Aborts process if mismatch — better than
    silent prod auth bypass.
    """
    runtime_env = os.environ.get("BOWHEAD_ENV", "dev").strip().lower()
    auth_mode = os.environ.get("BOWHEAD_AUTH_MODE", "prod")
    deployment_mode = os.environ.get("BOWHEAD_DEPLOYMENT_MODE", "local").strip().lower()
    dev_fallback = os.environ.get("BOWHEAD_DEV_USER_FALLBACK", "0") == "1"
    if auth_mode == "prod" and dev_fallback:
        raise RuntimeError("FATAL: BOWHEAD_DEV_USER_FALLBACK=1 not allowed when BOWHEAD_AUTH_MODE=prod. Set fallback=0 or change mode to dev.")
    # licensing §7.3 (SEC-8/CMP-5): a licensed local deployment must never run with the dev-user fallback —
    # it would let an unauthenticated caller assume an identity (and thus an team) past the entitlement gate.
    if deployment_mode == "local" and dev_fallback:
        raise RuntimeError("FATAL: BOWHEAD_DEV_USER_FALLBACK=1 not allowed when BOWHEAD_DEPLOYMENT_MODE=local. Set fallback=0.")
    if (
        runtime_env == "prod"
        and auth_mode == "prod"
        and deployment_mode in {"saas", "prod", "production"}
        and os.environ.get("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG") != "1"
    ):
        key = os.environ.get("BOWHEAD_FERNET_KEY", "")
        try:
            from cryptography.fernet import Fernet

            Fernet(key.encode())
        except Exception as e:
            raise RuntimeError(
                "FATAL: BOWHEAD_FERNET_KEY is required and must be a valid "
                "Fernet key when BOWHEAD_AUTH_MODE=prod for SaaS team LLM config "
                "secrets. Set BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG=1 only for "
                "explicit non-production recovery."
            ) from e
    # §C (real-auth): a dev fallback that survives BOTH hard-aborts above
    # (auth_mode=dev + deployment_mode=saas) STILL bypasses password auth — it lets an
    # unauthenticated caller assume the dev_default_user identity. That was the original
    # passwordless-login root cause; the prod/local aborts don't cover the saas+dev combo. A licensed
    # SaaS box (116) MUST run auth_mode=prod — make a stray dev fallback impossible to miss.
    if auth_mode == "dev" and dev_fallback:
        log.critical(
            "DEV AUTH BYPASS ACTIVE: BOWHEAD_AUTH_MODE=dev + BOWHEAD_DEV_USER_FALLBACK=1 — "
            "unauthenticated requests assume the dev_default_user identity (no password). "
            "NEVER run this on a shared or internet-facing deployment. "
            "Set BOWHEAD_AUTH_MODE=prod (or unset BOWHEAD_DEV_USER_FALLBACK).",
        )


def assert_sandbox_backend_safe() -> None:
    """sandbox-hardening 2026-06-17 (Phase 2a) startup guard: prod must NOT run the
    subprocess sandbox backend.

    ``BOWHEAD_SANDBOX_BACKEND=subprocess`` runs the agent's generated Python directly
    on the gateway host with NO container isolation (runner.py ``_run_subprocess``,
    "NO isolation by design" — dev/CI emergency only). On a prod box that is a full
    host-RCE primitive. Mirror ``assert_prod_auth_safe``: fail loud at boot rather
    than silently running uncontained user code. Called from main.py at startup.
    """
    runtime_env = os.environ.get("BOWHEAD_ENV", "dev").strip().lower()
    backend = (os.environ.get("BOWHEAD_SANDBOX_BACKEND") or "executor").strip().lower()
    if runtime_env == "prod" and backend == "subprocess":
        raise RuntimeError(
            "FATAL: BOWHEAD_SANDBOX_BACKEND=subprocess not allowed when "
            "BOWHEAD_ENV=prod — the subprocess backend has NO sandbox isolation "
            "(runs agent code directly on the host). Set "
            "remove BOWHEAD_SANDBOX_BACKEND (or set it to executor)."
        )
    if runtime_env == "prod" and backend != "executor":
        raise RuntimeError(
            "FATAL: unsupported BOWHEAD_SANDBOX_BACKEND in production — only executor is supported. Remove the variable or set it to executor."
        )


def assert_sandbox_executor_safe() -> None:
    """Production sandbox runs must use the executor and require its credentials.

    The gateway never owns a Docker socket or a sandbox image. If the URL/token are unset, the gateway would
    still boot and serve, but every sandbox run would silently fail closed
    ("sandbox 未就绪") — ops might wrongly believe de-DooD is protecting a working
    sandbox. Fail loud at boot instead (mirrors ``assert_prod_auth_safe``). Reachability
    is intentionally NOT probed here (the executor may come up moments after the app);
    a missing/empty token or URL is the misconfig we can catch deterministically.
    """
    runtime_env = os.environ.get("BOWHEAD_ENV", "dev").strip().lower()
    if runtime_env == "prod":
        url = (os.environ.get("BOWHEAD_SANDBOX_EXECUTOR_URL") or "").strip()
        token = (os.environ.get("BOWHEAD_SANDBOX_EXECUTOR_TOKEN") or "").strip()
        if not url or not token:
            raise RuntimeError(
                "FATAL: sandbox executor requires both BOWHEAD_SANDBOX_EXECUTOR_URL "
                "and BOWHEAD_SANDBOX_EXECUTOR_TOKEN to be "
                "set — without them the gateway cannot reach/authenticate the executor "
                "and every sandbox run fails closed. Set both (same token as the "
                "sandbox-executor service) in the deployment .env."
            )


def _email_verify_required() -> bool:
    """§C (real-auth) gate: when BOWHEAD_EMAIL_VERIFY_REQUIRED=1, an unverified
    email/password user cannot obtain or use a session (login + register + every
    authenticated route). Default 0 → 101 existing users unchanged. Read fresh
    (uncached) so per-deployment env + tests take effect.
    """
    from bowhead_gateway.auth_policy import EmailVerificationPolicy, resolve_account_policy

    return resolve_account_policy().email_verification is not EmailVerificationPolicy.NOT_REQUIRED


def _email_verify_enabled() -> bool:
    """Whether verification mails are actually sent. REQUIRED implies ENABLED — otherwise a
    REQUIRED=1 + ENABLED=0 misconfig would brick signups (gate ON but no link ever sent →
    nobody can ever verify). So an enforced deployment always sends the mail.
    """
    from bowhead_gateway.auth_policy import EmailVerificationPolicy, resolve_account_policy

    return (
        os.environ.get("BOWHEAD_EMAIL_VERIFY_ENABLED", "1") == "1"
        or resolve_account_policy().email_verification is EmailVerificationPolicy.SMTP_REQUIRED
    )


def _register_sends_verify_email() -> bool:
    """Whether registration mails a verification link on its own.

    Under ``not_required`` nothing depends on the address being verified, so registration sends
    nothing, whatever ``BOWHEAD_EMAIL_VERIFY_ENABLED`` says; a member can still verify from their
    profile, which uses the signed-in resend.
    """
    from bowhead_gateway.auth_policy import EmailVerificationPolicy, resolve_account_policy

    if resolve_account_policy().email_verification is EmailVerificationPolicy.NOT_REQUIRED:
        return False
    return _email_verify_enabled()


# Endpoints an unverified user must still reach — to BECOME verified (resend),
# sign out (logout), or read their own state (me). Matched by path suffix; the
# /auth prefix keeps these unique, and sub-paths like /auth/me/workspaces stay gated.
_VERIFY_EXEMPT_SUFFIXES: tuple[str, ...] = (
    "/auth/me",
    "/auth/logout",
    "/auth/logout-everywhere",
    "/auth/resend-verify",
)


def _is_verify_exempt_path(path: str) -> bool:
    return any(path.endswith(s) for s in _VERIFY_EXEMPT_SUFFIXES)


def _multi_team_authz_enabled() -> bool:
    """Feature gate for the multi-Team authorization migration.

    The database migration is always safe to run; only this switch changes
    request-time Team resolution. Keep it default-off until the rollout gates
    in docs/design/授权体系.md have completed.
    """
    return os.environ.get("FEATURE_MULTI_TEAM_AUTHZ", "0").strip().lower() in {"1", "true", "yes"}


async def _resolve_current_team(request: Request, user: User, store: UserStore) -> User:
    if not _multi_team_authz_enabled():
        from bowhead_gateway.auth_policy import invite_only_activation_enabled

        if invite_only_activation_enabled() and not await store.user_has_active_team_membership(
            user.id,
            user.team_id,
        ):
            raise HTTPException(403, {"code": "team_membership_required"})
        return user
    requested = (request.headers.get("X-Team-Id") or user.active_team_id or user.team_id).strip()
    if not requested or not await store.user_has_active_team_membership(user.id, requested):
        raise HTTPException(403, {"code": "team_membership_required"})
    # Routes still consume user.team_id. Project the verified current Team into
    # that compatibility field instead of trusting a JWT/header claim directly.
    return replace(user, team_id=requested, active_team_id=requested)


async def _authenticated_identity(request: Request) -> User:
    """Extract a globally active identity without requiring a Team membership.

    Dev-only escape hatch: when both ``auth_mode='dev'`` AND env
    ``BOWHEAD_DEV_USER_FALLBACK=1`` are set, missing-JWT requests fall back
    to ``settings.dev_default_user`` so local dev can hit chat/canvas paths
    without register/login. **Never enable in prod** — gating behind an
    explicit env (default OFF) is the safety boundary; ι-16 review flagged
    that ambient ``auth_mode='dev'`` alone was scope-creep beyond Sprint ι.
    """
    token: str | None = None
    cookie = request.cookies.get("bowhead_jwt")
    if cookie:
        token = cookie
    elif (auth := request.headers.get("Authorization")) and auth.startswith("Bearer "):
        token = auth[7:]
    if not token:
        settings = request.app.state.settings
        if _dev_user_fallback_enabled(request):
            store = _store(request)
            hdr = request.headers.get("x-bowhead-user-id")
            uid = hdr.strip() if hdr and hdr.strip() else settings.dev_default_user
            user = await store.get_user(uid)
            if user is None:
                from bowhead_core.user_store import User as _U

                user = _U(
                    id=uid,
                    team_id="",
                    email=f"{uid}@dev.local",
                    email_verified=True,
                    tier="ultra",
                    status="active",
                    credit_balance=999_999,
                    created_at="2026-01-01T00:00:00Z",
                )
            return user
        raise HTTPException(401, {"code": "auth_required"})
    payload = _decode_jwt_multi(token)  # P4.5.19 multi-key
    if not payload or not payload.get("sub"):
        raise HTTPException(401, {"code": "invalid_token"})
    from bowhead_gateway.edition_identity import external_session_allowed

    store = _store(request)
    generation = payload.get("asg")
    if isinstance(generation, str) and not await external_session_allowed(request.app, store, generation):
        raise HTTPException(401, {"code": "oidc_session_revoked"})
    user = await store.get_user(payload["sub"])
    # Sprint β G10 P0-S-3: reject stale pwd_v (forced relogin after password change)
    token_pwd_v = int(payload.get("pwd_v", 0) or 0)
    if user and token_pwd_v < int(getattr(user, "pwd_version", 0) or 0):
        raise HTTPException(401, {"code": "password_changed_relogin_required"})
    if not user or user.status != "active":
        raise HTTPException(403, {"code": "user_inactive"})
    # §C (real-auth): defense-in-depth — when verification is REQUIRED, no authenticated
    # route serves an unverified user (a cookie issued before the flag flipped, or any other
    # path) except the recovery endpoints they need to become verified / sign out / read self.
    if _email_verify_required() and not user.email_verified and not _is_verify_exempt_path(request.url.path):
        raise HTTPException(403, {"code": "email_not_verified"})
    return user


async def _current_user(request: Request) -> User:
    """Authenticated identity projected into a verified current Team."""
    user = await _authenticated_identity(request)
    user = await _resolve_current_team(request, user, _store(request))
    await _bind_public_entry_for_signed_in_user(request, user, _store(request))
    # Downstream projections in this request (e.g. KB citation read permissions) use the viewer, without reparsing the token.
    request.state.bowhead_user = user
    return user


async def _bind_current_team_public_entry(
    request: Request,
    team_id: str,
) -> None:
    from bowhead_gateway.public_entry_context import bind_team_public_entry

    await bind_team_public_entry(request, team_id=team_id)


async def _bind_public_entry_for_signed_in_user(
    request: Request,
    user: User,
    store: UserStore,
) -> None:
    """Bind a matching entry when one is configured for the signed-in team."""
    from bowhead_gateway.public_entry_context import bind_team_public_entry

    team_id = user.team_id
    await bind_team_public_entry(request, team_id=team_id)


# ─── Sprint α G9: role-gated Depends helpers ────────────


async def _audit_403_denial(
    user: User,
    target: str,
    role: str | None,
    allowed_roles: tuple[str, ...] | list[str],
    store: UserStore,
) -> None:
    """Rate-limited (5/min/user) audit row for 403 denials."""
    bucket = f"denial:uid:{user.id}"
    try:
        if await store.rate_limit_check(bucket, limit=5, window_s=60):
            await store.log_audit(
                user_id=user.id,
                team_id=user.team_id,
                event_type="endpoint_403_denial",
                payload={
                    "target": target,
                    "your_role": role or "none",
                    "required_roles": list(allowed_roles),
                },
            )
    except Exception:  # noqa: S110
        # Audit failure must NOT block the 403 from being raised
        pass


def _require_workspace_role(*allowed_roles: str):
    """FastAPI Depends factory.

    Verifies caller is a member of `workspace_id` (path param) with role in
    `allowed_roles`. Hierarchy: admin > member > viewer.

    Sec P0-S-6 fix: re-SELECT user from DB (not JWT cached attributes).
    Sec P1-S-13 fix: cross-team check — workspace must be in user's team.
    """

    async def dep(
        workspace_id: str,
        request: Request,
        store: UserStore = Depends(_store),
    ) -> User:
        user = await _current_user(request)
        # Current Team is resolved from an active membership, never from a
        # stale JWT team_id claim.
        if not user.team_id or not await store.user_has_active_team_membership(user.id, user.team_id):
            raise HTTPException(401, {"code": "user_not_active"})

        # P1-S-13: verify ws is in user's current team
        # NOTE: workspace_store separate — we rely on workspace_members membership
        # since workspaces table not in UserStore. Membership implies same team.
        role = await store.get_member_role(workspace_id, user.id)
        if role is None or not await store.workspace_in_team(user.team_id, workspace_id):
            await _audit_403_denial(user, f"ws:{workspace_id}", None, allowed_roles, store)
            raise HTTPException(403, {"code": "not_workspace_member"})
        if not await store.workspace_in_team(
            user.team_id,
            workspace_id,
            active_only=True,
        ):
            raise HTTPException(409, {"code": "workspace_archived"})

        required_min = min(
            (workspace_role_rank(normalize_workspace_role(r)) if normalize_workspace_role(r) else 99 for r in allowed_roles),
            default=99,
        )
        if workspace_role_rank(role) < required_min:
            await _audit_403_denial(user, f"ws:{workspace_id}", role, allowed_roles, store)
            raise HTTPException(
                403,
                {
                    "code": "insufficient_role",
                    "required": list(allowed_roles),
                    "your_role": role,
                },
            )
        return user

    return dep


def _require_team_role_dep(*allowed_roles: str):
    """FastAPI Depends factory.

    Team roles: owner, team_admin, audit_admin.
    Owner-only gates remain distinct from promoted team_admin.
    """

    async def dep(
        request: Request,
        store: UserStore = Depends(_store),
    ) -> User:
        user = await _current_user(request)
        # Re-SELECT to detect status flip post-JWT-issue
        fresh = await store.get_user(user.id)
        if not fresh or fresh.status != "active":
            raise HTTPException(401, {"code": "user_not_active"})

        # Supports 'owner' + 'team_admin' + 'audit_admin'.
        # P3-T-1: reject 'member' (use _current_user for that)
        if "member" in allowed_roles:
            raise RuntimeError("_require_team_role_dep does not accept 'member' (use _current_user)")

        # SP-AUTH-3: resolve roles in _current_user's current team (user.team_id was projected by
        # _resolve_current_team to a team with verified membership), not fresh.team_id
        # (users.team_id's primary team), which would check the wrong team in multi-team use.
        # get_team_role returns "owner" for the team owner, otherwise the active
        # team_members role. Keep owner-only gates distinct from promoted team_admin.
        fresh = replace(fresh, team_id=user.team_id, active_team_id=user.active_team_id)
        role = await store.get_team_role(fresh.id, fresh.team_id)
        if role and role in allowed_roles:
            return fresh

        await _audit_403_denial(fresh, f"team:{fresh.team_id}", role or "member", allowed_roles, store)
        raise HTTPException(
            403,
            {
                "code": "insufficient_role",
                "required": list(allowed_roles),
                "your_role": role or "member",
            },
        )

    return dep


async def _require_space_admission(
    workspace_id: str,
    request: Request,
    store: UserStore = Depends(_store),
) -> User:
    """「谁能把人放进空间」的唯一判定 (概念术语审计 P1-3, SP-AUTH-5).

    判据 = 当前团队 owner/team_admin ∪ 该空间 ws_admin; 空间必须属于当前团队
    (fail-closed)。空间邀请族端点共用本依赖; 管理面直加成员 (admin_members 的
    workspace-grants, 团队角色门 + workspace_in_team) 是本判定的子集, 两入口
    口径由此收敛。新增进空间入口时必须复用本依赖, 禁止自拼判据。
    """
    user = await _current_user(request)
    if not user.team_id or not await store.user_has_active_team_membership(user.id, user.team_id):
        raise HTTPException(401, {"code": "user_not_active"})
    _admission_roles = ("ws_admin", "team_admin", "owner")
    if not await store.workspace_in_team(user.team_id, workspace_id):
        await _audit_403_denial(user, f"ws:{workspace_id}", None, _admission_roles, store)
        raise HTTPException(403, {"code": "not_workspace_member"})
    if not await store.workspace_in_team(user.team_id, workspace_id, active_only=True):
        raise HTTPException(409, {"code": "workspace_archived"})
    team_role = await store.get_team_role(user.id, user.team_id)
    if team_role in {"owner", "team_admin"}:
        return user
    role = await store.get_member_role(workspace_id, user.id)
    if normalize_workspace_role(role) == "ws_admin":
        return user
    await _audit_403_denial(user, f"ws:{workspace_id}", role, _admission_roles, store)
    raise HTTPException(
        403,
        {
            "code": "insufficient_role",
            "required": list(_admission_roles),
            "your_role": role or "none",
        },
    )


async def _require_space_governance_read(
    workspace_id: str,
    request: Request,
    store: UserStore = Depends(_store),
) -> User:
    """Authorize invitation governance reads without requiring an active Space."""

    user = await _current_user(request)
    if not user.team_id or not await store.user_has_active_team_membership(user.id, user.team_id):
        raise HTTPException(401, {"code": "user_not_active"})
    governance_roles = ("ws_admin", "team_admin", "owner")
    if not await store.workspace_in_team(user.team_id, workspace_id):
        await _audit_403_denial(
            user,
            f"ws:{workspace_id}",
            None,
            governance_roles,
            store,
        )
        raise HTTPException(403, {"code": "not_workspace_member"})
    team_role = await store.get_team_role(user.id, user.team_id)
    if team_role in {"owner", "team_admin"}:
        return user
    role = await store.get_member_role(workspace_id, user.id)
    if normalize_workspace_role(role) == "ws_admin":
        return user
    await _audit_403_denial(
        user,
        f"ws:{workspace_id}",
        role,
        governance_roles,
        store,
    )
    raise HTTPException(
        403,
        {
            "code": "insufficient_role",
            "required": list(governance_roles),
            "your_role": role or "none",
        },
    )


# Convenience aliases
_require_workspace_admin = _require_workspace_role("admin")
_require_team_admin_dep = _require_team_role_dep("owner", "team_admin")


# ─── rate limit (in-memory token bucket, 5 / min / IP) ─────


_RATE_BUCKET: dict[str, list[float]] = {}
_RATE_LIMIT = 5
_RATE_WINDOW_SEC = 60


def _check_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    bucket = _RATE_BUCKET.setdefault(ip, [])
    # drop old entries
    bucket[:] = [t for t in bucket if now - t < _RATE_WINDOW_SEC]
    if len(bucket) >= _RATE_LIMIT:
        raise HTTPException(429, {"code": "rate_limited"})
    bucket.append(now)


async def _enqueue_verify_email(
    request: Request,
    store: UserStore,
    user_id: str,
    email: str,
    team_id: str | None = None,
) -> None:
    """Mint a fresh verify-token jti, build the verify URL, enqueue the SMTP row.

    Shared by register (initial send), /resend-verify (session) and
    /resend-verify-public (by-email). The CALLER decides WHETHER to send (unverified
    + BOWHEAD_EMAIL_VERIFY_ENABLED + rate limit); this just performs the send.

    email-config sprint: team_id 透传给 outbox(worker 按行解析 team-level SMTP)。
    """
    import uuid

    from bowhead_core.email_sender import make_verify_token

    if team_id is None:
        raise RuntimeError("verification email requires a team")
    new_jti = uuid.uuid4().hex
    token = make_verify_token(
        user_id,
        email,
        _email_verify_secret_keys()[0],
        ttl_hours=24 * 7,
        jti=new_jti,
    )
    from bowhead_gateway.identity_email import enqueue_rendered_identity_email, render_identity_email

    # Build the link first: without a public address it raises PublicEntryRequiredError before the
    # previous link is invalidated by rotating the jti.
    rendered = await render_identity_email(
        request,
        team_id=team_id,
        kind="verify",
        action_path=f"/api/v1/auth/verify-email?token={token}",
    )
    await store.update_verify_token_jti(user_id, new_jti)
    await enqueue_rendered_identity_email(store, rendered, to_email=email, purpose="verify")


_REGISTER_RECEIPT_PURPOSE: Final = "register_verify_receipt"
_REGISTER_RECEIPT_TTL_SECONDS: Final = 15 * 60


def _register_receipt_keys() -> list[str]:
    """Purpose-scoped keys for register receipts, derived from the JWT keyring (no new secret)."""
    return [secret for _key_id, secret in derive_jwt_secret_keys("register-verify-receipt")]


def _issue_register_receipt(user_id: str, email: str) -> str | None:
    """Short-lived proof that this browser just created ``user_id``.

    It only lets ``/resend-verify-public`` tell the registrant the real mail outcome. It is signed
    with its own derived key, so no session, verification-link or reset decoder accepts it.
    """
    keys = _register_receipt_keys()
    if not keys:
        return None
    now = int(time.time())
    payload = {
        "purpose": _REGISTER_RECEIPT_PURPOSE,
        "sub": user_id,
        "email": email,
        "iat": now,
        "exp": now + _REGISTER_RECEIPT_TTL_SECONDS,
    }
    return jwt.encode(payload, keys[0], algorithm="HS256")


def _register_receipt_matches(receipt: str | None, user: User) -> bool:
    """Whether ``receipt`` is a valid, unexpired register receipt for exactly this account."""
    if not receipt or len(receipt) > 2048:
        return False
    for key in _register_receipt_keys():
        try:
            payload = jwt.decode(receipt, key, algorithms=["HS256"], options={"require": ["exp", "sub"]})
        except jwt.PyJWTError:
            continue
        return payload.get("purpose") == _REGISTER_RECEIPT_PURPOSE and payload.get("sub") == user.id and payload.get("email") == user.email
    return False


def _diagnostic_reference(request: Request) -> str:
    import uuid

    from bowhead_gateway.http_request_context import request_id_from_scope

    return request_id_from_scope(request.scope) or uuid.uuid4().hex


async def _send_verify_email_outcome(
    request: Request,
    store: UserStore,
    user: User,
    *,
    log_event: str,
) -> dict[str, str]:
    """Queue the verification mail and report what actually happened.

    ``queued`` means the outbox row exists and a mail service is configured for it; delivery by
    the outbox worker is not observed here. Every failure keeps the account and is logged with the
    request id, which the registrant can quote to an administrator.
    """
    from bowhead_core.email_config import resolve_smtp_config

    from bowhead_gateway.public_url import PublicEntryRequiredError

    reason = "delivery_unavailable"
    try:
        await _enqueue_verify_email(request, store, user.id, user.email, team_id=user.team_id)
        # Mirror the outbox worker (strict team config): a row it cannot deliver is not "queued".
        # The row stays queued, so it is still delivered if mail is configured while it retries.
        if await resolve_smtp_config(user.team_id, store, strict_team=True) is not None:
            return {"status": "queued"}
        reason = "email_not_configured"
    except PublicEntryRequiredError:
        reason = "public_entry_required"
    except Exception:
        reference = _diagnostic_reference(request)
        log.exception("%s reason=delivery_unavailable team_id=%s reference=%s", log_event, user.team_id, reference)
        return {"status": "not_sent", "reason": reason, "reference": reference}
    reference = _diagnostic_reference(request)
    log.warning("%s reason=%s team_id=%s reference=%s", log_event, reason, user.team_id, reference)
    return {"status": "not_sent", "reason": reason, "reference": reference}


# ─── routes ────────────────────────────────────────────


class RegisterBody(BaseModel):
    email: str
    password: str = Field(..., min_length=8, max_length=200)
    display_name: str | None = None
    # Deprecated: public registration may not self-join an existing team by id.
    # Existing-team joins must come through invite_token or domain provisioning.
    team_id: str | None = None
    team_name: str | None = None
    # P4.5.18: invitation to an existing team (path B), mutually exclusive with team_id/team_name.
    invite_token: str | None = None

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:
        v = v.lower().strip()
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v


class InvitePreviewBody(BaseModel):
    token: str = Field(..., min_length=1, max_length=5000)


class InviteAcceptBody(BaseModel):
    token: str | None = Field(None, min_length=1, max_length=5000)


@router.post("/invites/preview")
async def preview_invite(
    body: InvitePreviewBody,
    request: Request,
    response: Response,
    store: UserStore = Depends(_store),
) -> dict:
    ip = request.client.host if request.client else "unknown"
    if not await store.rate_limit_check(f"invite_preview:{ip}", limit=30, window_s=60):
        raise HTTPException(429, {"code": "rate_limit"})
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"

    decode_status, payload = _decode_invite_jwt(body.token)
    if decode_status == "expired":
        return {"status": "expired"}
    if decode_status != "ok" or payload is None:
        return {"status": "invalid"}

    resolved = await _resolve_invite_row(store, body.token, payload)
    if not resolved:
        return {"status": "invalid"}

    invite_type = resolved["invite_type"]
    row = resolved["row"]
    preview_status = _invite_row_status(row)
    if preview_status != "valid":
        return {"status": preview_status, "invite_type": invite_type}

    from bowhead_core._email_utils import mask_email

    team_id = row.get("team_id")
    workspace = None
    if invite_type == "workspace":
        workspace = await store.get_workspace(str(row.get("workspace_id") or ""))
        team_id = row.get("team_id") or (workspace or {}).get("team_id")
    team = await store.get_team(str(team_id or ""))

    out: dict[str, Any] = {
        "status": "valid",
        "invite_type": invite_type,
        "invited_email_mask": mask_email(str(row.get("invited_email") or "")),
        "expires_at": row.get("expires_at"),
        "team": {"name": team.name if team else f"{product_name()} team"},
    }
    if invite_type == "workspace":
        out["workspace"] = {
            "name": (workspace or {}).get("name") or f"{product_name()} workspace",
            "role": row.get("role") or "member",
        }
    from bowhead_gateway.invite_flow import inviter_display_name

    inviter = await inviter_display_name(store, row, str(team_id or ""))
    if inviter:
        out["inviter_display_name"] = inviter

    viewer_payload = _extract_jwt_payload(request)
    viewer_user = await store.get_user(str(viewer_payload.get("sub"))) if viewer_payload and viewer_payload.get("sub") else None
    if viewer_user and viewer_user.status == "active" and (viewer_user.email_verified or not _email_verify_required()):
        viewer_email = viewer_user.email.lower().strip()
        invited_email = str(row.get("invited_email") or "").lower().strip()
        state = "logged_in_mismatched_email" if viewer_email != invited_email else "logged_in_matching_email"
        if state == "logged_in_matching_email" and str(team_id or "") != viewer_user.team_id:
            state = "logged_in_wrong_team"
        out["viewer_context"] = {
            "state": state,
            "email_mask": mask_email(viewer_email),
        }
    else:
        out["viewer_context"] = {"state": "anonymous"}
    return out


@router.post("/invites/accept")
async def accept_invite(
    body: InviteAcceptBody,
    request: Request,
    response: Response,
    user: User = Depends(_authenticated_identity),
    store: UserStore = Depends(_store),
) -> dict:
    if body.token is None:
        from bowhead_gateway.auth_mutation_security import require_same_origin_json
        from bowhead_gateway.auth_policy import invite_only_activation_enabled
        from bowhead_gateway.invite_flow import InviteFlowError, load_invite_context
        from bowhead_gateway.routes.invite_activation import (
            bind_invite_public_entry,
            clear_invite_cookie,
            invite_cookie_value,
        )

        if not invite_only_activation_enabled():
            raise HTTPException(404, {"code": "feature_disabled"})
        require_same_origin_json(request)
        try:
            context = await load_invite_context(
                store,
                raw_secret=invite_cookie_value(request),
            )
        except InviteFlowError as exc:
            raise HTTPException(exc.status_code, {"code": exc.code}) from exc
        await bind_invite_public_entry(request, context.team_id)
        if user.email.lower().strip() != context.invited_email:
            raise HTTPException(403, {"code": "invite_email_mismatch"})
        try:
            accepted = await store.accept_invite_from_intent_atomic(
                intent_hash=context.intent_hash,
                invite_type=context.invite_type,
                invite_id=context.invite_id,
                token_version=context.token_version,
                user_id=user.id,
                target_team_id=context.team_id,
                workspace_id=context.workspace_id,
                workspace_role=str(context.row.get("role") or "member"),
                multi_team_enabled=_multi_team_authz_enabled(),
            )
        except EntitlementDenied as exc:
            raise seat_denied_to_http(exc) from exc
        except ValueError as exc:
            code = "invite_cross_team_not_allowed" if "cross-team" in str(exc) else "invite_accept_failed"
            raise HTTPException(409, {"code": code}) from exc
        from bowhead_gateway.space_assignment_notifications import (
            resolve_space_assignment_if_ready,
        )

        await resolve_space_assignment_if_ready(
            request,
            store=store,
            team_id=context.team_id,
            user_id=user.id,
        )
        clear_invite_cookie(response)
        team = await store.get_team(context.team_id)
        has_workspace_access = bool(
            await store.list_workspaces_for_user(
                user.id,
                team_id=context.team_id,
            )
        )
        result = _build_invite_result(
            invite_type=context.invite_type,
            row=context.row,
            requires_verification=False,
            workspace_name=context.workspace_name,
            team_id=context.team_id,
            team_name=team.name if team else None,
            accept_status=str(accepted.get("status") or "accepted"),
            has_workspace_access=has_workspace_access,
        )
        await store.log_audit(
            user_id=user.id,
            team_id=context.team_id,
            event_type="invite_accept_success",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            payload={
                "invite_type": context.invite_type,
                "accept_status": accepted.get("status"),
                "transport": "server_intent",
            },
        )
        return {"ok": True, "invite_result": result}

    token = body.token
    decode_status, payload = _decode_invite_jwt(token)
    if decode_status == "expired":
        raise HTTPException(410, {"code": "invite_expired"})
    if decode_status != "ok" or payload is None:
        raise HTTPException(401, {"code": "invite_invalid"})

    resolved = await _resolve_invite_row(store, token, payload)
    if not resolved:
        raise HTTPException(401, {"code": "invite_invalid"})
    row = resolved["row"]
    row_status = _invite_row_status(row)
    if row_status != "valid":
        status_code = 410 if row_status == "expired" else 409
        raise HTTPException(status_code, {"code": f"invite_{row_status}"})

    invite_type = resolved["invite_type"]
    invited_email = str(row.get("invited_email") or "").lower().strip()
    if invited_email != user.email.lower().strip():
        raise HTTPException(403, {"code": "invite_email_mismatch"})

    workspace_name: str | None = None
    workspace_id: str | None = None
    if invite_type == "workspace":
        workspace_id = str(row.get("workspace_id") or "")
        workspace = await store.get_workspace(workspace_id)
        workspace_name = (workspace or {}).get("name")
        resolved_team_id = row.get("team_id") or (workspace or {}).get("team_id")
        if not resolved_team_id or resolved_team_id != user.team_id:
            raise HTTPException(403, {"code": "invite_cross_team_not_allowed"})
    elif row.get("team_id") != user.team_id:
        raise HTTPException(403, {"code": "invite_cross_team_not_allowed"})
    team = await store.get_team(user.team_id)

    try:
        accepted = await store.accept_invite_for_user_atomic(
            invite_type=invite_type,
            invite_id=str(row.get("id") or ""),
            user_id=user.id,
            team_id=user.team_id,
            email=user.email,
            workspace_id=workspace_id,
            workspace_role=str(row.get("role") or "member") if invite_type == "workspace" else None,
        )
    except ValueError as exc:
        raise HTTPException(409, {"code": "invite_accept_failed"}) from exc

    from bowhead_gateway.space_assignment_notifications import (
        resolve_space_assignment_if_ready,
    )

    await resolve_space_assignment_if_ready(
        request,
        store=store,
        team_id=user.team_id,
        user_id=user.id,
    )
    has_workspace_access = bool(
        await store.list_workspaces_for_user(
            user.id,
            team_id=user.team_id,
        )
    )
    await store.log_audit(
        user_id=user.id,
        team_id=user.team_id,
        event_type="invite_accept_success",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        payload={
            "invite_type": invite_type,
            "accept_status": accepted.get("status"),
            "invite_resolution": resolved.get("resolution"),
        },
    )

    return {
        "ok": True,
        "invite_result": _build_invite_result(
            invite_type=invite_type,
            row=row,
            requires_verification=False,
            workspace_name=workspace_name,
            team_id=user.team_id,
            team_name=team.name if team else None,
            accept_status=str(accepted.get("status") or "accepted"),
            has_workspace_access=has_workspace_access,
        ),
    }


class LoginBody(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:
        return v.lower().strip()


@router.post("/register")
async def register(
    body: RegisterBody,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    store: UserStore = Depends(_store),
) -> dict:
    _check_rate_limit(request)
    from bowhead_gateway.auth_policy import (
        EmailVerificationPolicy,
        invite_only_activation_enabled,
        resolve_account_policy,
        self_registration_allowed,
    )

    account_policy = resolve_account_policy(
        deployment_mode_override=getattr(request.app.state, "deployment_mode", None),
    )
    if body.invite_token and invite_only_activation_enabled():
        raise HTTPException(409, {"code": "invite_activation_required"})
    # feedback-triage-2026-08-24: criterion moved to auth_policy.self_registration_allowed (full rationale there).
    # The gate originally preceded invitation validation, returning 403 even for valid invite holders in INVITE_ONLY
    # deployments despite the complete invite_token enrollment path a few dozen lines below.
    if not self_registration_allowed(account_policy, has_invite_token=bool(body.invite_token)):
        raise HTTPException(
            403,
            detail={
                "code": ("registration_disabled_local" if account_policy.deployment_mode == "local" else "registration_disabled"),
                "message": "当前部署不开放自助注册，请使用邀请激活或联系管理员",
            },
        )
    if account_policy.email_verification is EmailVerificationPolicy.TRUSTED_IDP_REQUIRED:
        raise HTTPException(403, {"code": "trusted_idp_required"})
    # An operator can disable all new-account creation, including invitation joins.
    # Re-enable registration explicitly before using invitation onboarding.
    if os.environ.get("BOWHEAD_REGISTRATION_DISABLED", "0") == "1":
        await store.log_audit(
            user_id=None,
            team_id=None,
            event_type="register_blocked_disabled",
            ip=request.client.host if request.client else None,
            payload={"email_mask": __import__("bowhead_core._email_utils", fromlist=["mask_email"]).mask_email(body.email)},
        )
        raise HTTPException(403, "self-registration is disabled on this deployment")
    _validate_password_strength(body.password)  # P4.5.21
    existing = await store.get_user_by_email(body.email)
    if existing:
        await store.log_audit(
            user_id=None,
            team_id=None,
            event_type="register_dup",
            ip=request.client.host if request.client else None,
            payload={"email_mask": __import__("bowhead_core._email_utils", fromlist=["mask_email"]).mask_email(body.email)},
        )
        raise HTTPException(409, "email already registered")

    # P4.5.18: invite_token path B (invited to existing team / space).
    invite_payload: dict | None = None
    invite_type: str | None = None
    invite_resolution: dict[str, Any] | None = None
    team_invite_row: dict | None = None
    ws_invite_row: dict | None = None
    invite_workspace_name: str | None = None
    if body.invite_token:
        decode_status, invite_payload = _decode_invite_jwt(body.invite_token)
        if decode_status != "ok" or not invite_payload:
            raise HTTPException(401, "invite token decode failed")
        invite_resolution = await _resolve_invite_row(store, body.invite_token, invite_payload)
        if not invite_resolution or _invite_row_status(invite_resolution["row"]) != "valid":
            raise HTTPException(401, "invite token invalid / revoked / expired")
        invite_type = invite_resolution["invite_type"]
        # The JWT above is already signature + expiry verified. New links resolve to one
        # persisted invite row by signed invite_id; older links can only pass through the
        # constrained active-valid legacy fallback. Revoke/consume/expiry still gate on the
        # stored row, and the register email must match the signed invited_email below.
        invited_email = (invite_payload.get("invited_email") or "").lower().strip()
        if invite_type == "team":
            team_invite_row = invite_resolution["row"]
        else:
            ws_invite_row = invite_resolution["row"]
        # Defense-in-depth: the email is SIGNED into the invite JWT, so it must equal the
        # register email. (No `invited_email and …` guard — an empty signed email should
        # fail closed against any real body.email; this is a belt-and-suspenders 403.)
        if invited_email != body.email:
            raise HTTPException(403, "invite email does not match register email")

    if body.team_id:
        from bowhead_core._email_utils import mask_email

        await store.log_audit(
            user_id=None,
            team_id=None,
            event_type="register_rejected",
            ip=request.client.host if request.client else None,
            payload={
                "reason": "direct_team_join_disabled",
                "email_mask": mask_email(body.email),
            },
        )
        raise HTTPException(
            400,
            detail={
                "code": "direct_team_join_disabled",
                "message": "joining an existing team requires an invite",
            },
        )

    # Sprint α G6 (r4): white-list domain → MEMBER tier (NOT admin per P0-S-7).
    # Team selection precedence: invite > white-list match > new team.
    is_auto_member = is_auto_member_email(body.email)

    if team_invite_row is not None:
        team = await store.get_team(team_invite_row.get("team_id") or "")
        if not team:
            raise HTTPException(404, "invited team not found")
        team_id = team.id
    elif ws_invite_row is not None:
        # team_id comes from the workspace invite row already resolved at the gate, not a
        # second hash/email re-lookup. Single source.
        workspace = await store.get_workspace(str(ws_invite_row.get("workspace_id") or ""))
        invite_workspace_name = (workspace or {}).get("name")
        resolved_team_id = ws_invite_row.get("team_id") or (workspace or {}).get("team_id")
        team = await store.get_team(resolved_team_id or "")
        if not team:
            raise HTTPException(404, "invited team not found")
        team_id = team.id
    elif is_auto_member:
        # G6 r4: allowlisted domain -> join pre-provisioned team. NO auto-create (Sec P0-S-7).
        target_team = await store.find_default_team_for_domain(body.email)
        if target_team is None:
            from bowhead_core._email_utils import mask_email

            await store.log_audit(
                user_id=None,
                team_id=None,
                event_type="register_rejected",
                ip=request.client.host if request.client else None,
                payload={
                    "reason": "whitelist_domain_no_team_provisioned",
                    "email_mask": mask_email(body.email),
                    "domain": body.email.rsplit("@", 1)[-1].lower(),
                },
            )
            # Code-only: the localized copy comes from config/i18n/errors via the error
            # handler. A hardcoded ``message`` would short-circuit that and ship Chinese
            # to an English session. The rejected domain stays in the audit row above.
            raise HTTPException(
                400,
                detail={"code": "whitelist_domain_no_team_provisioned"},
            )
        team = target_team
        team_id = team.id
    else:
        team = await store.create_team(name=body.team_name or body.email.split("@")[1])
        team_id = team.id

    # Tier: white-list → ultra; otherwise team default
    final_tier = "ultra" if is_auto_member else team.default_user_tier
    # G6 r4: white-list domain skips email verify (verified=True at create).
    # §C (real-auth): when email-verify is REQUIRED (116 SaaS), NOBODY is auto-verified —
    # even whitelist domains (datarc.cn) must click the verify link. Flag default 0 keeps
    # the G6 behavior on 101 so existing/new datarc auto-members stay frictionless there.
    email_verified_init = is_auto_member and not _email_verify_required()
    invite_after_verify_next_route: str | None = None
    if invite_resolution and invite_type and _email_verify_required() and not email_verified_init:
        post_verify_result = _build_invite_result(
            invite_type=invite_type,
            row=invite_resolution["row"],
            requires_verification=False,
            workspace_name=invite_workspace_name,
            team_id=team_id,
            team_name=team.name,
        )
        invite_after_verify_next_route = str(post_verify_result["next_route"])

    try:
        if invite_resolution and invite_type:
            user = await store.create_invited_user_atomic(
                invite_type=invite_type,
                invite_id=str(invite_resolution["row"]["id"]),
                team_id=team_id,
                email=str(body.email),
                password=body.password,
                display_name=body.display_name,
                tier=final_tier,
                email_verified=email_verified_init,
                workspace_id=(str(ws_invite_row.get("workspace_id")) if ws_invite_row is not None else None),
                workspace_role=(str(ws_invite_row.get("role") or "member") if ws_invite_row is not None else None),
                invite_after_verify_next_route=invite_after_verify_next_route,
            )
        else:
            user = await store.create_user(
                team_id=team_id,
                email=str(body.email),
                password=body.password,
                display_name=body.display_name,
                tier=final_tier,
                email_verified=email_verified_init,
            )
    except EntitlementDenied as exc:  # seat gate (§4.2) — only raises when ENFORCE=1
        raise seat_denied_to_http(exc) from exc
    except sqlite3.IntegrityError as exc:
        # TOCTOU: the get_user_by_email pre-check 409s the common case, but a concurrent
        # same-email registration can still lose the users.email UNIQUE race here. Surface
        # the same clean 409 instead of a 500 (applies to both invite and normal paths).
        await store.log_audit(
            user_id=None,
            team_id=None,
            event_type="register_dup",
            ip=request.client.host if request.client else None,
            payload={"email_mask": __import__("bowhead_core._email_utils", fromlist=["mask_email"]).mask_email(body.email), "via": "insert_race"},
        )
        raise HTTPException(409, "email already registered") from exc
    except ValueError as exc:
        if invite_resolution:
            raise HTTPException(401, "invite token invalid / revoked / expired") from exc
        raise
    # Seed automatic language preferences and preserve a pre-signup UI choice.
    with contextlib.suppress(Exception):
        from bowhead_core.new_user_defaults import seed_new_user_settings

        await seed_new_user_settings(
            getattr(request.app.state, "user_settings_store", None),
            user.id,
            ui_language=request.cookies.get("NEXT_LOCALE"),
        )

    invite_has_workspace_access = False
    if invite_resolution and invite_type:
        from bowhead_gateway.space_assignment_notifications import (
            resolve_space_assignment_if_ready,
        )

        await resolve_space_assignment_if_ready(
            request,
            store=store,
            team_id=team_id,
            user_id=user.id,
        )
        invite_has_workspace_access = bool(
            await store.list_workspaces_for_user(
                user.id,
                team_id=team_id,
            )
        )
        if invite_after_verify_next_route is not None:
            finalized_next_route = str(
                _build_invite_result(
                    invite_type=invite_type,
                    row=invite_resolution["row"],
                    requires_verification=False,
                    workspace_name=invite_workspace_name,
                    team_id=team_id,
                    team_name=team.name,
                    has_workspace_access=invite_has_workspace_access,
                )["next_route"]
            )
            if finalized_next_route != invite_after_verify_next_route:
                await store.set_user_preference_key(
                    user.id,
                    "invite_after_verify_next_route",
                    finalized_next_route,
                )

    # G6 r4: Bootstrap creator role — only when team has no creator yet.
    # Order matters: check is_auto_member BEFORE self-create (auto_member also has no body.team_id).
    became_creator = False
    is_empty_team_bootstrap = False
    if invite_payload:
        pass  # invited path — never bootstrap creator
    elif is_auto_member:
        # White-list domain path — bootstrap creator only if team has no creator yet
        existing_creator = await store.get_team_creator_id(team_id)
        if existing_creator is None:
            is_empty_team_bootstrap = True
    else:
        # Self-create new team path — caller IS first user
        is_empty_team_bootstrap = True

    if is_empty_team_bootstrap:
        became_creator = await store.set_team_creator(team_id, user.id)
    # Audit action breakdown
    if is_empty_team_bootstrap and became_creator:
        audit_action = "domain_auto_admin_bootstrap" if is_auto_member else "register_team_creator"
    elif is_empty_team_bootstrap and not became_creator:
        # Race lost — another user beat us to creator
        audit_action = "bootstrap_race_lost_member"
    elif is_auto_member:
        audit_action = "domain_auto_member_added"
    elif team_invite_row is not None:
        audit_action = "team_invite_consumed"
    elif invite_payload:
        audit_action = "invite_consumed"
    else:
        audit_action = "register_member"

    # P4.5.18 / invite landing: invited registration side effects are handled
    # atomically inside UserStore.create_invited_user_atomic().

    await store.log_audit(
        user_id=user.id,
        team_id=team_id,
        event_type="register_success",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        payload={
            "via_invite": bool(invite_payload),
            "audit_action": audit_action,
            "invite_type": invite_type,
            "invite_resolution": (invite_resolution.get("resolution") if invite_resolution else None),
            "whitelist_domain": is_auto_member,
            "is_empty_team_bootstrap": is_empty_team_bootstrap,
        },
    )

    # Sprint α G3: enqueue verify email (skip if auto-verified per G6 whitelist)
    verification_mail: dict[str, str] | None = None
    if not user.email_verified and _register_sends_verify_email():
        # The account already exists: an unaddressable link, an outbox write failure or missing
        # mail configuration must not turn registration into a 500. The outcome is reported only
        # in this response, to the registrant.
        verification_mail = await _send_verify_email_outcome(request, store, user, log_event="auth.register.verify_email_unavailable")

    # §C (real-auth): when email-verify is REQUIRED, an unverified user must NOT get a usable
    # session — return requires_verification so the FE shows "check your email" instead of
    # auto-logging-in. The verify mail was just enqueued above; /resend-verify-public re-sends.
    # Flag default 0 → 101 keeps the auto-login (register issues the cookie below).
    if _email_verify_required() and not user.email_verified:
        await store.log_audit(
            user_id=user.id,
            team_id=team_id,
            event_type="register_requires_verification",
            ip=request.client.host if request.client else None,
        )
        out = {
            "requires_verification": True,
            "user": {
                "id": user.id,
                "email": user.email,
                "tier": user.tier,
                "team_id": user.team_id,
                "email_verified": False,
            },
        }
        if verification_mail is not None:
            out["verification_mail"] = verification_mail
            receipt = _issue_register_receipt(user.id, user.email)
            if receipt:
                out["verification_receipt"] = receipt
        if invite_resolution and invite_type:
            invite_result = _build_invite_result(
                invite_type=invite_type,
                row=invite_resolution["row"],
                requires_verification=True,
                workspace_name=invite_workspace_name,
                team_id=team_id,
                team_name=team.name,
                has_workspace_access=invite_has_workspace_access,
            )
            out["invite_result"] = invite_result
        return out

    # P4.5.27: access + refresh dual cookie
    token, _refresh = await _issue_token_pair(user, request, response, store)
    from bowhead_gateway.post_login_hooks import schedule_post_login_hooks

    await schedule_post_login_hooks(
        background_tasks,
        request,
        user=user,
        team_id=user.active_team_id or user.team_id,
        auth_method="register",
    )
    out = {
        "user": {
            "id": user.id,
            "email": user.email,
            "tier": user.tier,
            "team_id": user.team_id,
            "credit_balance": user.credit_balance,
            "email_verified": user.email_verified,
        },
        "token": token,
    }
    if invite_resolution and invite_type:
        out["invite_result"] = _build_invite_result(
            invite_type=invite_type,
            row=invite_resolution["row"],
            requires_verification=False,
            workspace_name=invite_workspace_name,
            team_id=team_id,
            team_name=team.name,
            has_workspace_access=invite_has_workspace_access,
        )
    return out


@router.post("/login")
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    store: UserStore = Depends(_store),
) -> dict:
    _check_rate_limit(request)
    user = await store.get_user_by_email(body.email)
    if not user or not user.password_hash:
        await store.log_audit(
            user_id=None,
            team_id=None,
            event_type="login_fail",
            ip=request.client.host if request.client else None,
            payload={
                "email_mask": __import__("bowhead_core._email_utils", fromlist=["mask_email"]).mask_email(body.email),
                "reason": "user_not_found",
            },
        )
        raise HTTPException(401, "invalid credentials")
    from bowhead_core.user_store import verify_password

    if not verify_password(body.password, user.password_hash):
        await store.log_audit(
            user_id=user.id,
            team_id=user.team_id,
            event_type="login_fail",
            ip=request.client.host if request.client else None,
            payload={"reason": "bad_password"},
        )
        raise HTTPException(401, "invalid credentials")
    if user.status != "active":
        raise HTTPException(403, f"user {user.status}")

    # §C (real-auth): enforce email verification when REQUIRED (116 SaaS). Password was
    # already verified above; we refuse the session and tell the FE to surface the verify
    # prompt + resend. Flag default 0 → 101 login is unchanged.
    if _email_verify_required() and not user.email_verified:
        await store.log_audit(
            user_id=user.id,
            team_id=user.team_id,
            event_type="login_blocked_unverified",
            ip=request.client.host if request.client else None,
        )
        raise HTTPException(403, {"code": "email_not_verified", "email": user.email})

    await store.update_last_active(user.id)
    await store.log_audit(
        user_id=user.id,
        team_id=user.team_id,
        event_type="login_success",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    # P4.5.27: access + refresh dual cookie
    token, _refresh = await _issue_token_pair(user, request, response, store)
    # A new device immediately receives the saved language; otherwise browser defaults make the preference seem unsaved.
    await _sync_locale_cookie(request, response, user.id)
    from bowhead_gateway.post_login_hooks import schedule_post_login_hooks

    await schedule_post_login_hooks(
        background_tasks,
        request,
        user=user,
        team_id=user.active_team_id or user.team_id,
        auth_method="password",
    )
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "tier": user.tier,
            "team_id": user.team_id,
            "credit_balance": user.credit_balance,
        },
        "token": token,
    }


@router.post("/refresh")
async def refresh_token(
    request: Request,
    response: Response,
    store: UserStore = Depends(_store),
) -> dict:
    """P4.5.27: 用 refresh cookie 旋转: revoke 旧 + issue 新 access + 新 refresh.

    safe replay: 同一 raw_token 只能用一次, rotate 是 atomic.
    """
    raw = request.cookies.get(_REFRESH_COOKIE)
    if not raw:
        raise HTTPException(401, "no refresh cookie")
    user_id = await store.verify_refresh_token(raw)
    if not user_id:
        if await store.refresh_token_rotated_within(raw, seconds=_REFRESH_ROTATION_GRACE_SEC):
            # Another tab of this browser rotated the shared cookie moments ago.
            raise _refresh_superseded_error()
        # Revoked / expired -> force new login.
        _clear_refresh_cookie(response)
        response.delete_cookie(key="bowhead_jwt")
        raise HTTPException(401, "refresh token invalid / revoked / expired")
    user = await store.get_user(user_id)
    if not user or user.status != "active":
        _clear_refresh_cookie(response)
        response.delete_cookie(key="bowhead_jwt")
        raise HTTPException(403, "user inactive")

    # §C (real-auth): refresh must NOT keep an unverified session alive when verification is
    # REQUIRED — mirror the login gate. A refresh token issued before the flag flipped (or before
    # an admin un-verified the user) is exactly the "any other path" the _current_user chokepoint
    # guards; here we also REVOKE it so the half-session can't roll its 30d TTL forward forever.
    if _email_verify_required() and not user.email_verified:
        await store.revoke_refresh_token(raw)
        _clear_refresh_cookie(response)
        response.delete_cookie(key="bowhead_jwt")
        raise HTTPException(403, {"code": "email_not_verified", "email": user.email})

    from datetime import datetime, timedelta

    # V1 refresh tokens carry a non-credential generation. Preserve it across
    # one-shot rotation; a legacy token starts a new fenced chain exactly once.
    auth_session_generation = _refresh_auth_session_generation(raw) or _new_auth_session_generation()
    from bowhead_gateway.edition_identity import external_session_allowed

    if not await external_session_allowed(request.app, store, auth_session_generation):
        await store.revoke_refresh_token(raw)
        _clear_refresh_cookie(response)
        response.delete_cookie(key="bowhead_jwt")
        raise HTTPException(401, {"code": "oidc_session_revoked"})
    await _register_composer_auth_generation(
        request,
        user_id=user.id,
        generation=auth_session_generation,
    )
    new_refresh = _new_refresh_token(auth_session_generation)
    expires_at = (datetime.now(UTC) + timedelta(seconds=_REFRESH_TTL_SEC)).isoformat()
    rotated = await store.rotate_refresh_token(
        old_raw=raw,
        new_raw=new_refresh,
        user_id=user.id,
        expires_at=expires_at,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    if not rotated:
        if await store.refresh_token_rotated_within(raw, seconds=_REFRESH_ROTATION_GRACE_SEC):
            # A concurrent request from another tab won the one-shot rotation.
            # Its response already carries the successor cookies; do not revoke
            # the family or clear the shared cookies.
            raise _refresh_superseded_error()
        # Race: someone used this token first -> revoke all (compromise defense).
        await store.revoke_all_refresh_for_user(user.id)
        # Both racers registered/reused the same Composer generation before
        # the one-shot rotate CAS. Revoke that generation family as part of
        # the compromise boundary, otherwise the winner's freshly issued
        # access token remains able to submit despite "all sessions revoked".
        await _invalidate_all_composer_auth_generations(
            request,
            user_id=user.id,
        )
        _clear_refresh_cookie(response)
        response.delete_cookie(key="bowhead_jwt")
        raise HTTPException(401, "refresh race / replay detected, all sessions revoked")

    new_access = _issue_jwt(
        user,
        _jwt_secret(request),
        auth_session_generation=auth_session_generation,
        auth_time=0,
    )
    _set_jwt_cookie(response, new_access)
    _set_refresh_cookie(response, new_refresh)
    return {
        "ok": True,
        "user": {"id": user.id, "email": user.email, "tier": user.tier},
        "token": new_access,
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    store: UserStore = Depends(_store),
) -> dict:
    generations_to_invalidate: set[tuple[str, str]] = set()
    invalidate_all_for_users: set[str] = set()
    payload = _extract_jwt_payload(request)
    if payload and payload.get("sub"):
        access_user_id = str(payload["sub"])
        # `_auth_session_generation` bridges both current `asg` tokens and
        # pre-Batch-A access tokens.  Reading only the cookie/asg pair here left
        # bearer-only and legacy sessions able to submit after logout.
        generations_to_invalidate.add(
            (access_user_id, _auth_session_generation(request)),
        )
        await store.log_audit(
            user_id=access_user_id,
            team_id=payload.get("team_id"),
            event_type="logout",
            ip=request.client.host if request.client else None,
        )
    # P4.5.27: also revoke refresh token + clear refresh cookie
    refresh_raw = request.cookies.get(_REFRESH_COOKIE)
    if refresh_raw:
        refresh_generation = _refresh_auth_session_generation(refresh_raw)
        refresh_user_id = await store.revoke_refresh_token_and_get_user(refresh_raw)
        if refresh_user_id and refresh_generation:
            generations_to_invalidate.add((refresh_user_id, refresh_generation))
        elif refresh_user_id:
            # A legacy refresh token does not encode the access-token bridge.
            # Fail closed for Composer by tombstoning every generation owned by
            # that user instead of silently leaving an unknown chain active.
            invalidate_all_for_users.add(refresh_user_id)
    for user_id in invalidate_all_for_users:
        await _invalidate_all_composer_auth_generations(request, user_id=user_id)
    for user_id, generation in generations_to_invalidate:
        if user_id in invalidate_all_for_users:
            continue
        await _invalidate_composer_auth_generation(
            request,
            user_id=user_id,
            generation=generation,
        )
    response.delete_cookie(key="bowhead_jwt")
    _clear_refresh_cookie(response)
    return {"ok": True}


@router.post("/logout-everywhere")
async def logout_everywhere(
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """P4.5.27: revoke 该 user 全部 refresh tokens (踢所有设备登出)."""
    n = await store.revoke_all_refresh_for_user(user.id)
    await _invalidate_all_composer_auth_generations(request, user_id=user.id)
    response.delete_cookie(key="bowhead_jwt")
    _clear_refresh_cookie(response)
    await store.log_audit(
        user_id=user.id,
        team_id=user.team_id,
        event_type="logout_everywhere",
        ip=request.client.host if request.client else None,
        payload={"revoked_count": n},
    )
    return {"ok": True, "revoked_count": n}


@router.get("/me")
async def me(
    request: Request,
    response: Response,
    identity: User = Depends(_authenticated_identity),
    store: UserStore = Depends(_store),
) -> dict:
    from bowhead_gateway.terminal_protocol_rollout import (
        chat_execution_writer_readiness,
        durable_chat_execution_config,
        resolve_chat_execution_admission,
        terminal_v1_attachment_refs_wire_version,
        terminal_v1_web_client_capable,
    )

    response.headers["Cache-Control"] = "no-store, private"
    # Repair existing sessions: set missing/mismatched account-language cookie (matching values do not resend Set-Cookie).
    await _sync_locale_cookie(request, response, identity.id)
    durable_chat_config = durable_chat_execution_config()
    durable_chat_readiness = chat_execution_writer_readiness(
        getattr(request.app.state, "terminal_writer_service", None),
        config=durable_chat_config,
    )
    try:
        user = await _resolve_current_team(request, identity, store)
        await _bind_public_entry_for_signed_in_user(request, user, store)
        await _register_composer_auth_generation(
            request,
            user_id=user.id,
            generation=_auth_session_generation(request),
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") != "team_membership_required":
            raise
        capabilities: dict[str, Any] = {
            "credit": {},
            "composer": {
                "submission_protocol": False,
                "reference_input": False,
                "reference_plan": False,
                "agent_start_use": False,
                "model_panel_v2": False,
                "thinking_effort": False,
            },
            "planning": False,
            "custom_branding": False,
            "team_onboarding": False,
            "space_admin_v2": False,
            "subagent_management_v2": False,
            "subagent_authoring": {
                "authoring": False,
                "draft_chat_edit": False,
            },
        }
        return {
            "id": identity.id,
            "email": identity.email,
            "email_verified": identity.email_verified,
            "has_password": bool(identity.password_hash),
            "oauth_providers": identity.oauth_providers,
            "tier": identity.tier,
            "team_id": "",
            "team_name": "",
            "display_name": identity.display_name,
            "avatar_url": identity.avatar_url,
            "team_role": "member",
            "capabilities": capabilities,
            "chat_execution": {
                "write_protocol": "unavailable",
                "config_generation": (durable_chat_config.config_generation),
                "readiness_generation": (durable_chat_readiness.readiness_generation),
            },
            **_capability_metadata(
                request,
                team_id="",
                capabilities=capabilities,
            ),
            "credit_capabilities": {},
            "credit_balance": identity.credit_balance,
            "credit_quota": identity.credit_quota,
            # Without an active Team membership no monthly reset applies to this account.
            "credit_period_start": None,
            "credit_period_resets_at": None,
            "locale": identity.locale,
            "timezone": identity.timezone,
            "auth_mode": getattr(
                getattr(request.app.state, "settings", None),
                "auth_mode",
                "prod",
            ),
            "is_team_admin": False,
            "workspace_count": 0,
            "needs_onboarding": False,
            "membership_required": True,
        }
    # P4.5.23: add is_team_admin for frontend onboarding differentiation.
    is_team_admin = await store.is_team_admin(user.id, user.team_id)
    team_role = await store.get_team_role(user.id, user.team_id) or "member"
    # Display name of the current team (sidebar identity row: "team · role"). Additive field; empty when the
    # team row is missing.
    current_team = await store.get_team(user.team_id)
    credit_capabilities = credit_capabilities_for_role(team_role).to_dict()
    member_rows = await store.list_member_workspaces(user.id)
    # workflow-solidify surfaces B/C gate: FE shows the Save-workflow chip + /settings/workflows
    # route only when the single BOWHEAD_CCA_WORKFLOW_CREATOR switch is on (same flag that
    # mounts the distill/team-skill routes + registers the trigger; no FE/BE drift).
    # content-permissions wave 2 — gates the /settings/access-requests tab.
    from bowhead_core.access_request_flags import access_requests_enabled
    from bowhead_core.composer_rollout import (
        composer_submission_protocol_enabled,
        resolve_composer_capabilities,
    )

    # Team subagent authoring and its Chat card use the shared capability projection.
    # Existing Agent runtime resolution and Marketplace readiness are independent.
    from bowhead_core.subagent_authoring_rollout import (
        resolve_subagent_authoring_capabilities,
    )
    from bowhead_core.subagent_management_flags import (
        subagent_management_v2_ready,
    )

    # cca P2 gate (G18, docs/skills-tools-ssot.md §5-G18), previously omitted: FE could not flag-gate
    # the Create-tool entry (create-tool card retained only the chat-trigger route).
    from bowhead_core.tool_creator_trigger import tool_creator_enabled
    from bowhead_core.workflow_creator_trigger import workflow_creator_enabled, workflow_solidify_entry_enabled

    from bowhead_gateway._entitlement_gate import is_feature_licensed_request

    subagent_authoring = resolve_subagent_authoring_capabilities(user.team_id)
    subagent_management_ready = await subagent_management_v2_ready(
        getattr(request.app.state, "agent_assignment_store", None),
        schema_ready=bool(
            getattr(request.app.state, "agent_marketplace_schema_ready", False),
        ),
    )
    from bowhead_gateway.chat_reference_start import reference_composer_authoring_ready

    composer = resolve_composer_capabilities(user.team_id)
    chat_execution = resolve_chat_execution_admission(
        config=durable_chat_config,
        team_id=user.team_id,
        session_facts=None,
        client_capable=terminal_v1_web_client_capable(request.headers),
        action_identity_complete=False,
        require_action_identity=False,
        existing_writer_ready=(durable_chat_readiness.existing_writer_ready),
        fresh_writer_ready=durable_chat_readiness.fresh_writer_ready,
    )
    from bowhead_gateway.chat_reference_start import reference_plan_writer_ready

    reference_input_ready = reference_composer_authoring_ready(request)
    agent_start_use_ready = reference_input_ready and os.environ.get("FEATURE_AGENT_START_USE_V1", "0").strip().lower() in {"1", "true", "yes"}
    from bowhead_core.shopify_content import content_writes_enabled

    shopify_enabled = _shopify_plugin_feature_on()
    capabilities = {
        "credit": credit_capabilities,
        "personal_mcp": await is_feature_licensed_request(request, "mcp"),
        # Members authorize Google under My connections; this is gated separately from the MCP license.
        "google_workspace": await _google_workspace_available(request, user.team_id),
        # Deployment switch only (admins need the help article before the team connection is enabled).
        "google_workspace_feature": _google_workspace_feature_on(),
        "shopify_plugin": shopify_enabled,
        # Deployment switch for the Coach alignment loop: its Chat cards, review lines and held-out controls.
        "agent_eval_alignment": _agent_eval_alignment_feature_on(),
        "shopify_content_confirmations": shopify_enabled and content_writes_enabled(),
        "composer": {
            "submission_protocol": composer_submission_protocol_enabled(),
            "reference_input": reference_input_ready,
            "reference_plan": reference_plan_writer_ready(request),
            "agent_start_use": agent_start_use_ready,
            "model_panel_v2": composer.model_panel_v2,
            "thinking_effort": composer.thinking_effort,
        },
        "planning": _planning_enabled(),
        "custom_branding": await is_feature_licensed_request(request, "custom_branding"),
        "workflow_creator": workflow_creator_enabled(),
        # Narrow capability bit for the Save-workflow **entry** (default off since 2026-09-01; see
        # workflow_solidify_entry_enabled). Separate from workflow_creator, the overall switch for
        # the entire feature family: hiding just this entry must preserve team Skill governance and
        # /settings/workflows management of already saved assets.
        "workflow_solidify": workflow_creator_enabled() and workflow_solidify_entry_enabled(),
        "subagent_creator": subagent_authoring.creator,
        "configuration_agent": True,
        "settings_pages_canvas": True,
        "configuration_canvas": subagent_authoring.unified_draft
        and os.environ.get("FEATURE_CONFIGURATION_CANVAS_V1", "1").strip().lower() in {"1", "true", "yes", "on"},
        "subagent_authoring": {
            "authoring": subagent_authoring.creator,
            "draft_chat_edit": subagent_authoring.draft_chat_edit,
            "unified_draft": subagent_authoring.unified_draft,
        },
        # Marketplace is a consumer surface independent from Agent authoring.
        # The create action remains gated separately by ``subagent_creator``.
        "subagent_management_v2": subagent_management_ready,
        "tool_creator": tool_creator_enabled(),
        "access_requests": access_requests_enabled(),
        # agent-feedback-loop FE batch A: suggestion-inbox entry follows the server gate (endpoints already return 404 when off).
        "feedback_loop": True,
        "team_onboarding": team_role in {"owner", "team_admin"},
        "space_admin_v2": space_admin_v2_enabled(),
        "kb_use_guard": use_guard_enabled(),
        "kb_health_observation": os.environ.get("FEATURE_KB_TRUST_V2", "0").strip().lower() in {"1", "true", "yes", "on"},
        # Settings › Account offers "Deutsch" as an answer language only while German model output is admitted;
        # PUT /user-settings/output_language keeps rejecting de-DE when it is not.
        "german_ai_output": german_ai_output_enabled(),
        "account_deletion": getattr(request.app.state, "account_deletion_service", None) is not None,
    }
    return {
        "id": user.id,
        "email": user.email,
        "email_verified": user.email_verified,
        "has_password": bool(user.password_hash),
        "oauth_providers": user.oauth_providers,
        "tier": user.tier,
        "team_id": user.team_id,
        "team_name": current_team.name if current_team else "",
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "team_role": team_role,
        "capabilities": capabilities,
        "chat_execution": {
            "write_protocol": chat_execution.write_protocol.value,
            "config_generation": durable_chat_config.config_generation,
            "readiness_generation": (durable_chat_readiness.readiness_generation),
            "attachment_refs_wire_version": (terminal_v1_attachment_refs_wire_version()),
        },
        **_capability_metadata(
            request,
            team_id=user.team_id,
            capabilities=capabilities,
        ),
        "credit_capabilities": credit_capabilities,
        "credit_balance": user.credit_balance,
        "credit_quota": user.credit_quota,
        # Own credit period only (this endpoint always describes the caller): when the current
        # period started and when the monthly reset refills the balance next.
        "credit_period_start": user.credit_period_start,
        "credit_period_resets_at": next_monthly_credit_reset().isoformat(),
        "locale": user.locale,
        "timezone": user.timezone,
        # tools sprint WP1d: deployment auth_mode so the FE can gate dev-only surfaces (e.g. the
        # local stdio-MCP block on /settings/tools is dev-only — RCE red line, hidden in prod).
        "auth_mode": getattr(getattr(request.app.state, "settings", None), "auth_mode", "prod"),
        # P4.5.23 onboarding signal.
        "is_team_admin": is_team_admin,
        "workspace_count": len(member_rows),
        "needs_onboarding": is_team_admin and len(member_rows) == 0,
    }


@router.post(
    "/me/space-assignment-request",
    status_code=202,
    dependencies=[Depends(require_not_readonly)],
)
async def request_space_assignment(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, str]:
    """Request Space access without exposing Team governance metadata."""

    if not await store.user_has_active_team_membership(user.id, user.team_id):
        raise HTTPException(401, {"code": "user_not_active"})
    if await store.list_workspaces_for_user(user.id, team_id=user.team_id):
        raise HTTPException(
            409,
            {"code": "space_access_already_available"},
        )
    recipients = await store.list_team_users_by_roles(
        user.team_id,
        {"owner", "team_admin"},
        include_creator=True,
    )
    if not recipients:
        raise HTTPException(
            409,
            {"code": "space_assignment_recipient_missing"},
        )
    service = getattr(request.app.state, "notification_service", None)
    if service is None:
        raise HTTPException(
            503,
            {"code": "notification_service_unavailable"},
        )
    item = await service.emit_space_assignment_requested(
        team_id=user.team_id,
        requester_user_id=user.id,
    )
    if item is None:
        raise HTTPException(
            503,
            {"code": "notification_service_unavailable"},
        )
    return {"status": "submitted"}


class LastTeamBody(BaseModel):
    team_id: str = Field(..., min_length=1, max_length=200)


@router.get("/me/teams")
async def list_my_teams(
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Return the caller's Team memberships and the verified active Team."""
    if not _multi_team_authz_enabled():
        role = await store.get_team_role(user.id, user.team_id) or "member"
        return {"items": [{"id": user.team_id, "name": "", "role": role}], "active_team_id": user.team_id}
    return {"items": await store.list_active_teams_for_user(user.id), "active_team_id": user.team_id}


@router.put("/me/last-team")
async def set_last_team(
    body: LastTeamBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, str]:
    if not _multi_team_authz_enabled():
        raise HTTPException(404, {"code": "multi_team_authz_disabled"})
    if not await store.user_has_active_team_membership(user.id, body.team_id):
        raise HTTPException(403, {"code": "team_membership_required"})
    redirect_url = ""
    from bowhead_gateway.public_entry_context import current_public_access_context
    from bowhead_gateway.public_url import PublicEntryRequiredError, public_app_url_for_base, team_public_entry

    context = current_public_access_context()
    try:
        target = await team_public_entry(request, team_id=body.team_id)
    except PublicEntryRequiredError:
        target = None
    except Exception:
        log.exception("team_switch.entry_metadata_unavailable team_id=%s", body.team_id)
        target = None
    if target is not None and (context is None or target.base_url != context.base_url):
        redirect_url = public_app_url_for_base(target.base_url, "/")
    if not await store.set_active_team(user.id, body.team_id):
        raise HTTPException(409, {"code": "team_switch_conflict"})
    result = {"ok": "true", "team_id": body.team_id}
    if redirect_url:
        result["redirect_url"] = redirect_url
    return result


# ─── Sprint β G2 — workspaces (D-1=B Bowhead canonical) ─────


@router.get("/me/spaces")  # Batch 3/c2: new canonical path.
@router.get("/me/workspaces")  # Legacy alias (retain one version).
async def list_my_workspaces(
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """Return persisted Space memberships and the preferred active Space."""
    items = await store.list_workspaces_for_user(user.id, team_id=user.team_id)
    active_id = (user.preferences or {}).get("last_workspace_id")
    # Validate active_id is in items (E-WS-7: ws deleted → FE falls to items[0])
    if active_id and not any(i["workspace_id"] == active_id for i in items):
        active_id = items[0]["workspace_id"] if items else None
    return {
        "items": items,
        "active_id": active_id,
    }


# ─── ws-datarc-decouple ② (R2.2 / #110): Bowhead-local workspace CRUD ─────
# Create / rename / archive a workspace WITHOUT touching datarc (the coupling that blocked
# #110 — "workspace == datarc project", no rename/create write API). BI is team-connection-gated
# at runtime (P3), so a local workspace just has no BI until the team adds a connection.


class CreateWorkspaceBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    set_as_current: bool = False


class UpdateWorkspaceBody(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    # `status` is the natural REST field; the class-scoped field doesn't rebind the
    # module-level `from fastapi import status` (F811 is a false positive for class attrs).
    status: str | None = None  # active | archived


@router.post("/me/spaces", status_code=201, dependencies=[Depends(require_not_readonly)])  # Batch 3/c2.
@router.post("/me/workspaces", status_code=201, dependencies=[Depends(require_not_readonly)])  # legacy alias
async def create_workspace(
    body: CreateWorkspaceBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """Create a Bowhead-local workspace in the caller's team; the caller becomes admin.
    No datarc backend (KB/docs/chat work immediately; BI appears if the team has a connection)."""
    if not user.team_id:
        raise HTTPException(400, {"code": "no_team", "message": "user has no team"})
    try:
        workspace = await store.create_governed_workspace(
            team_id=user.team_id,
            actor_user_id=user.id,
            name=body.name,
            description=body.description,
            set_as_current=body.set_as_current,
        )
    except SpaceGovernanceError as exc:
        status_code = 403 if exc.code == "not_team_admin" else 409
        if exc.code in {"workspace_name_required", "workspace_name_too_long"}:
            status_code = 422
        raise HTTPException(status_code, {"code": exc.code}) from exc
    from bowhead_gateway.space_assignment_notifications import (
        resolve_space_assignment_if_ready,
    )

    await resolve_space_assignment_if_ready(
        request,
        store=store,
        team_id=user.team_id,
        user_id=user.id,
    )
    return workspace


@router.patch("/me/spaces/{workspace_id}", dependencies=[Depends(require_not_readonly)])  # Batch 3/c2.
@router.patch("/me/workspaces/{workspace_id}", dependencies=[Depends(require_not_readonly)])  # legacy alias
async def update_workspace(
    workspace_id: str,
    body: UpdateWorkspaceBody,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """Legacy API for Space lifecycle compatibility.

    New UI uses Team governance routes for archive/restore and Space settings
    routes for metadata.  Preserve this wire endpoint without allowing a Team
    administrator to bypass explicit Space-admin checks for metadata.
    """
    if not user.team_id:
        raise HTTPException(400, {"code": "no_team", "message": "user has no team"})
    if body.status is not None and body.status not in ("active", "archived"):
        raise HTTPException(422, f"invalid status: {body.status!r}")
    if body.name is None and body.description is None and body.status is None:
        raise HTTPException(422, {"code": "empty_update", "message": "nothing to update"})
    try:
        current = await store.get_workspace(workspace_id)
        if current is None or current["team_id"] != user.team_id:
            raise SpaceGovernanceError("workspace_not_found")
        result: dict[str, Any] | None = None
        if body.status is not None:
            if not await store.is_team_admin(user.id, user.team_id):
                raise SpaceGovernanceError("not_team_admin")
            if body.status == "archived":
                result = await store.archive_governed_workspace(
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    workspace_id=workspace_id,
                    expected_revision=int(current["revision"] or 1),
                )
                from bowhead_gateway.routes.admin_spaces import pause_archived_space_jobs

                await pause_archived_space_jobs(request.app, workspace_id=workspace_id)
            else:
                result = await store.restore_governed_workspace(
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    workspace_id=workspace_id,
                    expected_revision=int(current["revision"] or 1),
                )
                from bowhead_gateway.routes.admin_spaces import resume_restored_space_jobs

                await resume_restored_space_jobs(request.app, workspace_id=workspace_id)
        if body.name is not None or "description" in body.model_fields_set:
            role = normalize_workspace_role(
                await store.get_member_role(workspace_id, user.id),
            )
            if role != "ws_admin":
                raise SpaceGovernanceError("not_workspace_admin")
            expected_revision = int(result["revision"]) if result is not None else int(current["revision"] or 1)
            result = await store.update_governed_workspace(
                team_id=user.team_id,
                actor_user_id=user.id,
                workspace_id=workspace_id,
                expected_revision=expected_revision,
                name=body.name,
                description=(body.description if "description" in body.model_fields_set else _UNSET),
            )
        return result or {"workspace_id": workspace_id, "ok": True}
    except SpaceGovernanceError as exc:
        status_code = {
            "workspace_not_found": 404,
            "not_team_admin": 403,
            "not_workspace_admin": 403,
            "workspace_archived": 409,
            "workspace_not_archived": 409,
            "last_active_space": 409,
            "restore_admin_required": 409,
            "duplicate_workspace_name": 409,
            "stale_workspace_update": 409,
        }.get(exc.code, 422)
        detail: dict[str, Any] = {"code": exc.code}
        if exc.details:
            detail["details"] = exc.details
        raise HTTPException(status_code, detail) from exc


class LastWorkspaceBody(BaseModel):
    workspace_id: str


@router.put("/me/last-space")  # Batch 3/c2: new canonical path.
@router.put("/me/last-workspace")  # Legacy alias (retain one version).
async def set_last_workspace(
    body: LastWorkspaceBody,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """G2 PUT /me/last-workspace.

    P1-S-4: IDOR guard — verify caller is active member of workspace_id.
    P1-S-3: atomic json_set (avoid read-modify-write loss).
    Also touches workspace_members.last_active_at.
    """
    role = await store.get_member_role(body.workspace_id, user.id)
    if not role or not await store.workspace_in_team(
        user.team_id,
        body.workspace_id,
        active_only=True,
    ):
        raise HTTPException(403, {"code": "not_workspace_member"})
    await store.set_user_preference_key(user.id, "last_workspace_id", body.workspace_id)
    await store.touch_workspace_member_active(body.workspace_id, user.id)
    return {"ok": True, "workspace_id": body.workspace_id, "role": role}


# ─── Sprint β G10 — change-password + active sessions ─────


class ChangePasswordBody(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=8, max_length=200)


def _mask_ip(ip: str | None) -> str:
    if not ip:
        return "unknown"
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.x"
    return ip[:8] + "..." if len(ip) > 8 else ip


@router.post("/change-password")
async def change_password_endpoint(
    body: ChangePasswordBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """G10 — change password with pwd_v JWT bump + revoke other sessions + re-issue.

    Order:
    1. OAuth-only check (P0-U-4 friendly early reject)
    2. Rate limit (P1-S-10 5/15min)
    3. Try acquire pwd_change lock (P2-S-5 30s)
    4. Verify old password (constant-time scrypt)
    5. Validate new password strength
    6. update_user_password — pwd_v + session_token_version bump
    7. Revoke all refresh tokens
    8. Re-issue current session token pair (so this tab keeps working)
    9. Enqueue notification email
    """
    # 1. OAuth-only early check (P0-U-4)
    if not user.password_hash:
        raise HTTPException(
            422,
            {
                "code": "oauth_user_no_password",
                "message": "你通过 OAuth 登录，没有本地密码可修改",
                "providers": user.oauth_providers,
            },
        )

    # 2. Rate limit (P1-S-10 tightened: 5/15min/user)
    if not await store.rate_limit_check(
        f"change_password:uid:{user.id}",
        limit=5,
        window_s=900,
    ):
        raise HTTPException(429, {"code": "too_many_password_attempts"})

    # 3. Lock (P2-S-5 — 30s)
    if not await store.try_acquire_pwd_change_lock(user.id, ttl_s=30):
        raise HTTPException(409, {"code": "concurrent_change_in_progress"})

    try:
        # 4. Verify old password (constant-time via sprint α verify_password)
        from bowhead_core.user_store import hash_password, verify_password

        if not verify_password(body.old_password, user.password_hash):
            await store.write_admin_audit_log(
                actor_user_id=user.id,
                team_id=user.team_id,
                action="password_change_failed",
                scope="user",
                payload={"reason": "wrong_old_password", "ip_masked": _mask_ip(request.client.host if request.client else None)},
            )
            raise HTTPException(401, {"code": "wrong_old_password"})

        # 5. Strength
        _validate_password_strength(body.new_password)

        # 6. update_user_password (atomic — also bumps pwd_version + session_token_version)
        new_hash = hash_password(body.new_password)
        ok = await store.update_user_password(user.id, new_hash)
        if not ok:
            raise HTTPException(500, {"code": "update_failed"})

        # Critical audit (D-3=B) — atomic with main op already done; write separately is OK
        # because we already committed update_user_password above.
        await store.write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action="password_change",
            scope="user",
            payload={"ip_masked": _mask_ip(request.client.host if request.client else None)},
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )

        # 7. Revoke all refresh tokens (current session re-issued below)
        revoked_count = await store.revoke_all_refresh_for_user(user.id)

        # 8. Re-issue token pair so this tab keeps working
        # Re-fetch fresh user (pwd_version was bumped)
        fresh_user = await store.get_user(user.id)
        new_token, _new_refresh = await _issue_token_pair(
            fresh_user,
            request,
            response,
            store,
        )

        # Notify through the durable SMTP outbox after revoking old sessions.
        with contextlib.suppress(Exception):
            from bowhead_gateway.error_handler import resolve_terminal_error_language

            name = product_name()
            masked_ip = _mask_ip(request.client.host if request.client else None)
            other_devices = max(0, revoked_count - 1)
            if resolve_terminal_error_language(request.scope) == "zh-CN":
                subject = f"{name} 密码已修改"
                message = (
                    f"你的 {name} 密码已被修改（IP：{masked_ip}）。\n\n"
                    f"如不是你本人，请立即联系管理员或重置密码。\n\n"
                    f"其他设备已退出登录，需用新密码重新登录（{other_devices} 个设备）。\n"
                )
            else:
                subject = f"Your {name} password has changed"
                message = (
                    f"Your {name} password has changed (IP: {masked_ip}).\n\n"
                    "If you did not make this change, contact your administrator or reset your password immediately.\n\n"
                    f"Other devices were signed out and must use your new password ({other_devices} devices).\n"
                )
            await store.enqueue_smtp(
                to_email=user.email,
                subject=subject,
                body=message,
                purpose="password_change_notification",
                team_id=user.team_id,
            )

        return {
            "ok": True,
            "access_token": new_token,
            "other_devices_kicked": max(0, revoked_count - 1),
        }
    finally:
        await store.release_pwd_change_lock(user.id)


@router.get("/sessions/active")
async def list_active_sessions(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """G10 — list active refresh-token sessions for current user.

    is_current flag set by comparing token_hash to current cookie's refresh hash.
    """
    sessions = await store.list_refresh_tokens(user.id, active_only=True)
    # Compute current session id from cookie
    current_refresh_raw = request.cookies.get("bowhead_refresh")
    import hashlib

    current_hash = None
    if current_refresh_raw:
        current_hash = hashlib.sha256(current_refresh_raw.encode()).hexdigest()
    items = []
    for s in sessions:
        items.append(
            {
                "session_id": s["id"],
                "expires_at": s["expires_at"],
                "created_at": s["created_at"],
                "last_used_at": s["last_used_at"],
                "user_agent": s.get("user_agent"),
                "ip_masked": _mask_ip(s.get("ip")),
                "is_current": s.get("token_hash") == current_hash if current_hash else False,
            }
        )
    return {"items": items, "current_identified": current_hash is not None}


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """G10 — revoke specific session (IDOR-safe via revoke_refresh_token_by_id)."""
    ok = await store.revoke_refresh_token_by_id(session_id, user.id)
    if not ok:
        raise HTTPException(404, {"code": "session_not_found"})
    await store.write_admin_audit_log(
        actor_user_id=user.id,
        team_id=user.team_id,
        action="session_revoked",
        target_kind="session",
        target_id=session_id,
        scope="user",
        payload={},
        ip=request.client.host if request.client else None,
    )
    return {"ok": True, "session_id": session_id}


@router.get("/onboarding-status")
async def onboarding_status(
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """P4.5.23: 显式 onboarding state, 前端 wizard 用.

    path 'admin' = 自建 team, 需要 add datarc workspace, 邀请 member.
    path 'invitee' = 受邀加入 (已有 workspace_member row).
    """
    is_admin = await store.is_team_admin(user.id, user.team_id)
    members = await store.list_member_workspaces(user.id)
    if is_admin:
        path = "admin"
        next_step = "add_workspace" if not members else "invite_members"
    else:
        path = "invitee"
        next_step = "ready"
    return {
        "path": path,
        "is_team_admin": is_admin,
        "workspace_count": len(members),
        "next_step": next_step,
    }


# ─── P4.5.14 password reset ────────────────────────────


class ForgotBody(BaseModel):
    email: str


# ─── Sprint α G3: email verify (link-based, 2-step POST, jti claim) ────


_VERIFY_LANDING_COPY = {
    "en-US": {
        "title": "Verify email — Bowhead",
        "heading": "Verify your email",
        "intro": "Select the button below to verify",
        "button": "Verify ✓",
        "verifying": "Verifying…",
        "success": "✓ Email verified",
        "verified": "Verified",
        "failed": "Verification failed",
        "retry": "Retry",
        "network_error": "Network error. Please try again.",
        "errors": {
            "verify_token_superseded": "A newer verification email replaced this link. Open the most recent Bowhead verification email and use its link.",
            "verify_token_expired_or_invalid": "This verification link is invalid or expired. Resend the verification email and try again.",
            "verify_failed": "Verification failed. Try again later or resend the verification email.",
        },
    },
    "zh-CN": {
        "title": "验证邮箱 — Bowhead",
        "heading": "验证你的邮箱",
        "intro": "点击下方按钮完成验证",
        "button": "验证 ✓",
        "verifying": "验证中…",
        "success": "✓ 邮箱已验证",
        "verified": "已验证",
        "failed": "验证失败",
        "retry": "重试",
        "network_error": "网络错误，请重试",
        "errors": {
            "verify_token_superseded": "这个验证链接已被更新的邮件取代。请打开你最新收到的「验证你的 Bowhead 账号」邮件，点里面的链接完成验证。",
            "verify_token_expired_or_invalid": "验证链接已失效或无效（可能已过期）。请重新发送验证邮件后再试。",
            "verify_failed": "验证失败，请稍后重试；若仍不行，请重新发送验证邮件。",
        },
    },
}


# Colors are the product default light scheme (theme_compiler.product_default_compiled_scheme).
_VERIFY_LANDING_HTML = r"""<!doctype html>
<html lang="{locale}"><head><meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Segoe UI", sans-serif;
    background: #F4F7FB; color: #061223; margin: 0;
    display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
  .card {{ background: #FFFFFF; border: 1px solid #A8B9D1; border-radius: 12px;
    padding: 32px 28px; max-width: 420px; width: 90%; text-align: center;
    box-shadow: 0 1px 2px rgba(0,0,0,.04); }}
  h1 {{ font-size: 22px; margin: 0 0 8px; }}
  p {{ color: #4F5F74; font-size: 14px; line-height: 1.6; margin: 4px 0 18px; }}
  .email {{ color: #061223; font-weight: 500; }}
  .btn {{ background: #1754DB; color: #fff; border: 0; border-radius: 6px;
    padding: 12px 24px; font-size: 14px; cursor: pointer; }}
  .btn:hover {{ background: #003ABC; }}
  .btn:disabled {{ opacity: 0.45; cursor: default; }}
  .msg {{ margin-top: 16px; font-size: 13px; }}
  .msg.ok {{ color: #16794A; }}
  .msg.err {{ color: #B42318; }}
</style></head>
<body>
<div class="card">
  <h1>{heading}</h1>
  <p>{intro} · <span class="email">{email}</span></p>
  <form id="vf">
    <input type="hidden" name="token" value="{token}">
    <button type="submit" class="btn">{button}</button>
  </form>
  <div id="msg" class="msg"></div>
</div>
<script>
(function() {{
  const copy = {copy_json};
  const configuredBasePath = {base_path_json};
  const internalPath = '/api/v1/auth/verify-email';
  const browserPath = window.location.pathname;
  const derivedCandidate = browserPath.endsWith(internalPath)
    ? browserPath.slice(0, -internalPath.length).replace(/\/+$/, '')
    : null;
  const derivedBasePath = derivedCandidate === '' || (
    derivedCandidate !== null
    && derivedCandidate.startsWith('/')
    && !derivedCandidate.startsWith('//')
    && !derivedCandidate.includes('//')
  ) ? derivedCandidate : null;
  const basePath = derivedBasePath === null ? configuredBasePath : derivedBasePath;
  const appPath = (path) => {{
    if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//')) return '/';
    if (!basePath || path === basePath || path.startsWith(basePath + '/') || path.startsWith(basePath + '?')) return path;
    return path === '/' ? basePath + '/' : basePath + path;
  }};
  const form = document.getElementById('vf');
  const msg = document.getElementById('msg');
  const btn = form.querySelector('button');
  form.addEventListener('submit', async function(e) {{
    e.preventDefault();
    btn.disabled = true;
    btn.textContent = copy.verifying;
    msg.className = 'msg';
    msg.textContent = '';
    const token = form.querySelector('[name=token]').value;
    try {{
      const r = await fetch(appPath('/api/v1/auth/verify-email'), {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{token}}),
      }});
      const d = await r.json().catch(() => ({{}}));
      if (r.ok) {{
        msg.className = 'msg ok';
        msg.textContent = copy.success;
        btn.textContent = copy.verified;
        try {{ new BroadcastChannel('bowhead-verify').postMessage({{type: 'verified'}}); }} catch (_) {{}}
        const nextRoute = typeof d.next_route === 'string' ? d.next_route : '/';
        setTimeout(() => window.location.replace(appPath(nextRoute)), 1500);
      }} else {{
        const code = (d.detail && d.detail.code) || d.detail || '';
        msg.className = 'msg err';
        msg.textContent = copy.errors[code] || (copy.failed + (code ? ' (' + code + ')' : ''));
        btn.disabled = false;
        btn.textContent = copy.retry;
      }}
    }} catch (err) {{
      msg.className = 'msg err';
      msg.textContent = copy.network_error;
      btn.disabled = false;
      btn.textContent = copy.retry;
    }}
  }});
}})();
</script>
</body></html>
"""


@router.get("/verify-email")
async def verify_email_landing(request: Request, token: str = "", store: UserStore = Depends(_store)) -> Response:
    """Sprint α G3: GET landing page — clean URL, no token in Referer.

    User clicks email link → this page renders form → user clicks button →
    POST /verify-email with token in body.
    """
    if not token:
        return Response(content="missing token", status_code=400)

    # Try decode to show email to user; don't fail loud (FE shows generic error)
    from bowhead_core.email_sender import decode_verify_token

    # Derive from every retained JWT key so rotation does not break outstanding links.
    payload = decode_verify_token(token, _email_verify_secret_keys())
    email_display = "<email hidden>"
    if payload and payload.get("email"):
        from bowhead_core._email_utils import mask_email

        email_display = mask_email(payload["email"])

    # HTML escape token for embedding (it's JWT base64url, safe but defensive)
    import html

    from bowhead_gateway.public_url import public_app_path

    app_root = public_app_path("/")
    base_path = "" if app_root == "/" else app_root.removesuffix("/")

    from bowhead_core.language_context import _normalize_accept_language

    preferred_locale = request.cookies.get("NEXT_LOCALE")
    if preferred_locale not in _UI_LOCALES:
        preferred_locale = _normalize_accept_language(request.headers.get("accept-language"))
    locale = "zh-CN" if preferred_locale == "zh-CN" else "en-US"
    copy = {
        key: {code: product_copy(message) for code, message in value.items()} if isinstance(value, dict) else product_copy(value)
        for key, value in _VERIFY_LANDING_COPY[locale].items()
    }
    html_body = _VERIFY_LANDING_HTML.format(
        locale=locale,
        title=copy["title"],
        heading=copy["heading"],
        intro=copy["intro"],
        button=copy["button"],
        copy_json=json.dumps(copy, ensure_ascii=True),
        base_path_json=json.dumps(base_path),
        email=html.escape(email_display),
        token=html.escape(token),
    )
    return Response(
        content=html_body,
        media_type="text/html; charset=utf-8",
        headers={
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-store",
        },
    )


class VerifyEmailBody(BaseModel):
    token: str


@router.post("/verify-email")
async def verify_email_consume(
    body: VerifyEmailBody,
    request: Request,
    store: UserStore = Depends(_store),
) -> dict:
    """Sprint α G3: POST consume verify token. Atomic via jti."""
    from bowhead_core.email_sender import decode_verify_token

    payload = decode_verify_token(body.token, _email_verify_secret_keys())
    if not payload or not payload.get("sub"):
        raise HTTPException(410, {"code": "verify_token_expired_or_invalid"})

    user_id = payload["sub"]
    jti = payload.get("jti")

    # Sec P1-S-7: check jti matches stored (anti-replay if user re-sent invalidates old)
    if jti is not None:
        stored_jti = await store.get_verify_token_jti(user_id)
        if stored_jti is not None and stored_jti != jti:
            raise HTTPException(410, {"code": "verify_token_superseded"})

    # Atomic mark — rowcount=1 only when flipped 0→1 with jti match
    updated = await store.mark_email_verified_atomic(user_id, jti)
    if not updated:
        # Idempotent re-click case: already verified earlier
        user = await store.get_user(user_id)
        if user and user.email_verified:
            out = {"ok": True, "status": "already_verified"}
            next_after_login = (user.preferences or {}).get("invite_after_verify_next_route")
            safe_next = _safe_next_path(next_after_login if isinstance(next_after_login, str) else None)
            if safe_next:
                out["next_route"] = f"/login?next={quote(safe_next, safe='')}"
            return out
        raise HTTPException(410, {"code": "verify_failed"})

    await store.log_audit(
        user_id=user_id,
        team_id=None,
        event_type="email_verified",
        ip=request.client.host if request.client else None,
    )

    out = {"ok": True, "status": "verified"}
    verified_user = await store.get_user(user_id)
    next_after_login = (verified_user.preferences or {}).get("invite_after_verify_next_route") if verified_user else None
    safe_next = _safe_next_path(next_after_login if isinstance(next_after_login, str) else None)
    if safe_next:
        out["next_route"] = f"/login?next={quote(safe_next, safe='')}"
    return out


@router.post("/resend-verify")
async def resend_verify(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """Sprint α G3: resend verify email. Rate-limited 1/min/user via SQLite."""
    if user.email_verified:
        return {"ok": True, "status": "already_verified"}

    bucket = f"resend_verify:uid:{user.id}"
    if not await store.rate_limit_check(bucket, limit=1, window_s=60):
        raise HTTPException(429, {"code": "resend_too_frequent", "wait_seconds": 60})

    from bowhead_gateway.public_url import PublicEntryRequiredError, public_entry_required_detail

    try:
        await _enqueue_verify_email(request, store, user.id, user.email, team_id=user.team_id)
    except PublicEntryRequiredError as exc:
        raise HTTPException(409, public_entry_required_detail()) from exc
    await store.log_audit(
        user_id=user.id,
        team_id=user.team_id,
        event_type="verify_email_resend",
        ip=request.client.host if request.client else None,
    )
    return {"ok": True}


class ResendPublicBody(BaseModel):
    email: str
    # Register receipt returned by /register; only it unlocks the real outcome below.
    receipt: str | None = Field(None, max_length=2048)

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:
        return v.lower().strip()


@router.post("/resend-verify-public")
async def resend_verify_public(
    body: ResendPublicBody,
    request: Request,
    store: UserStore = Depends(_store),
) -> dict:
    """§C (real-auth): resend the verify email WITHOUT a session.

    When BOWHEAD_EMAIL_VERIFY_REQUIRED=1 the register/login flows refuse a session to
    an unverified user, so the session-based /resend-verify is unreachable for them —
    this by-email variant lets them re-request the link from the login/register screen.

    No-leak (mirrors /forgot-password): ALWAYS returns 200; only enqueues when the email
    maps to an existing, unverified user and email-verify is enabled. Rate-limited
    5/min/IP (shared register/login bucket) + 1/min/user.

    Only a caller holding a valid, unexpired register receipt for this same account (returned by
    /register to the registrant) gets the real ``verification_mail`` outcome and an explicit
    cooldown; everyone else gets the identical ``{"ok": true}``.
    """
    _check_rate_limit(request)
    user = await store.get_user_by_email(body.email)
    if user and not user.email_verified and _email_verify_enabled():
        registrant = _register_receipt_matches(body.receipt, user)
        bucket = f"resend_verify:uid:{user.id}"
        if await store.rate_limit_check(bucket, limit=1, window_s=60):
            outcome = await _send_verify_email_outcome(request, store, user, log_event="auth.resend_verify_public.unavailable")
            if outcome["status"] == "queued":
                await store.log_audit(
                    user_id=user.id,
                    team_id=user.team_id,
                    event_type="verify_email_resend_public",
                    ip=request.client.host if request.client else None,
                )
            if registrant:
                return {"ok": True, "verification_mail": outcome}
        elif registrant:
            raise HTTPException(429, {"code": "resend_too_frequent", "wait_seconds": 60})
    return {"ok": True}


@router.post("/forgot-password")
async def forgot_password(
    body: ForgotBody,
    request: Request,
    store: UserStore = Depends(_store),
) -> dict:
    """Return success for an unknown address; otherwise report actual SMTP delivery."""
    _check_rate_limit(request)
    email = body.email.lower().strip()
    user = await store.get_user_by_email(email)
    if user is not None:
        from bowhead_core.email_config import deliver_email, resolve_smtp_config
        from bowhead_core.email_sender import make_password_reset_token

        from bowhead_gateway.identity_email import render_identity_email

        secret = _jwt_secret(request)
        token = make_password_reset_token(user.id, secret, ttl_hours=24)
        try:
            cfg = await resolve_smtp_config(user.team_id, store)
        except Exception:
            log.exception("password_reset.smtp_config_unavailable user_id=%s", user.id)
            raise HTTPException(503, {"code": "email_delivery_unavailable"}) from None
        if cfg is None:
            raise HTTPException(503, {"code": "email_not_configured"})

        try:
            rendered = await render_identity_email(
                request,
                team_id=user.team_id,
                kind="password_reset",
                action_path=f"/reset-password?flow=self_service&token={token}",
            )
            delivered, _ = await deliver_email(
                email,
                rendered.subject,
                rendered.text_body,
                config=cfg,
                html_body=rendered.html_body,
            )
        except Exception:
            log.exception("password_reset.delivery_error user_id=%s", user.id)
            delivered = False
        if not delivered:
            raise HTTPException(503, {"code": "email_delivery_unavailable"})
        await store.log_audit(
            user_id=user.id,
            team_id=user.team_id,
            event_type="password_reset_request",
            ip=request.client.host if request.client else None,
        )
    return {"ok": True, "message": "如果该邮箱存在, 重置链接已发送"}


class ResetBody(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8, max_length=200)


@router.post("/reset-password")
async def reset_password(
    body: ResetBody,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    store: UserStore = Depends(_store),
) -> dict:
    from bowhead_core.email_sender import decode_password_reset_token
    from bowhead_core.user_store import hash_password

    _validate_password_strength(body.new_password)  # P4.5.21
    payload = decode_password_reset_token(body.token, _jwt_secret(request))
    if not payload or not payload.get("sub"):
        raise HTTPException(410, {"code": "recovery_invalid_or_expired"})
    user_id = payload["sub"]
    new_hash = hash_password(body.new_password)
    # Keep credential and revocation state in one transaction. The store
    # mutator also bumps pwd/session-token versions and Terminal v1 auth epoch.
    if not await store.update_user_password(user_id, new_hash):
        raise HTTPException(410, {"code": "recovery_invalid_or_expired"})
    # A reset link proves control of the email address. Replace every prior
    # session with one for the recovering user, so they can continue directly.
    await store.revoke_all_refresh_for_user(user_id)
    user = await store.get_user(user_id)
    if user is None or user.status != "active":
        raise HTTPException(410, {"code": "recovery_invalid_or_expired"})
    token, _refresh = await _issue_token_pair(user, request, response, store)
    await _sync_locale_cookie(request, response, user.id)
    from bowhead_gateway.post_login_hooks import schedule_post_login_hooks

    await schedule_post_login_hooks(
        background_tasks,
        request,
        user=user,
        team_id=user.active_team_id or user.team_id,
        auth_method="password_reset",
    )
    await store.log_audit(
        user_id=user_id,
        team_id=None,
        event_type="password_reset_done",
        ip=request.client.host if request.client else None,
    )
    return {"ok": True, "token": token}


# ─── P4.5.16 official Datarc SSO (replaces P4.5.3 HMAC) ───


class DatarcSSOBody(BaseModel):
    """P4.5.16: Bowhead-primary auth → datarc 免登 (Bowhead user 已登录).

    workspace_id 可选; 不传则用 datarc 当前 workspace.
    """

    workspace_id: str | int | None = None


@router.post("/datarc-sso")
async def datarc_sso(
    body: DatarcSSOBody,
    request: Request,
    user: User = Depends(_current_user),
) -> dict:
    """P4.5.16: 用 Bowhead 已登录 user 调 datarc /openapi/v1/auth/auth-data/.

    返 {auth_data, login_url} — 前端用 login_url 加载 datarc iframe / 跳转免登.

    替 P4.5.3 HMAC 流 (Bowhead → datarc 单向; 不再走 datarc → Bowhead).
    """
    from bowhead_gateway.datarc_context import resolve_team_datarc_client

    datarc = await resolve_team_datarc_client(request, user.team_id)
    if datarc is None:
        raise HTTPException(503, "team datarc connection not configured")

    identity = user.email
    if not identity:
        raise HTTPException(400, "user has no email identity for datarc SSO")

    # Batch 2/c7 (PRD §3.2, review P0+P1): two per-user gates:
    # ① Trusted identity: no passwordless token for unverified self-reported email (old behavior issued iframe
    #   tokens for arbitrary email, enabling impersonation). Ladder (verified email / L3 admin mapping) without account -> 403.
    # ② Target-workspace ACL: body.workspace_id must belong to this user's accessible set (as strict as
    #   switch primitives; previously accepted client input unchecked, splitting the two surfaces).
    svc = getattr(request.app.state, "tool_context_service", None)
    if svc is None:
        raise HTTPException(503, "tool context service not configured")
    account_uid = await svc.resolve_trusted_account_uid(datarc, user.id)
    if not account_uid:
        raise HTTPException(403, "no trusted datarc identity — 请验证邮箱或联系管理员绑定北极九章账号")
    if body.workspace_id:
        refs = await svc.accessible_workspaces(datarc, user.id)
        if str(body.workspace_id) not in {r.id for r in refs}:
            raise HTTPException(403, "workspace not in your accessible set — 目标工作区不在你的可访问列表")

    try:
        auth_data = await datarc.get_sso_auth_data(
            identity=identity,
            project_id=body.workspace_id,
            urlencode=True,
        )
    except Exception as e:
        # Datarc may lack this user -> 401 so FE guides admin to add them.
        raise HTTPException(401, f"datarc SSO failed: {e}")

    connection_store = getattr(request.app.state, "datarc_connection_store", None)
    connections = await connection_store.list_connections(user.team_id) if connection_store is not None else []
    if not connections:
        raise HTTPException(503, "team datarc connection not configured")
    web_base = connections[0].base_url
    login_url = f"{web_base.rstrip('/')}/#/?authData={auth_data}"

    store: UserStore = request.app.state.user_store
    await store.log_audit(
        user_id=user.id,
        team_id=user.team_id,
        event_type="datarc_sso_issued",
        ip=request.client.host if request.client else None,
        payload={"workspace_id": str(body.workspace_id) if body.workspace_id else None},
    )

    return {"auth_data": auth_data, "login_url": login_url}


# ─── Batch 2/c12 (workspace-space-split PRD §2.3): zero-space onboarding data ───
# Per-user invitation query + tokenless acceptance (server knows session email == invited_email;
# no original JWT link reconstruction). Only verified email may accept; self-reported unverified email enables invitation hijacking.


@router.get("/me/invites")
async def list_my_invites(request: Request, user: User = Depends(_current_user)) -> dict:
    store: UserStore = request.app.state.user_store
    email = (user.email or "").strip()
    if not email:
        return {"items": []}
    rows = await store.list_active_invites_for_email(email)
    return {
        "items": [
            {
                "invite_id": r["id"],
                "workspace_id": r["workspace_id"],
                "workspace_name": r.get("workspace_name") or r["workspace_id"],
                "role": r.get("role") or "member",
                "expires_at": r.get("expires_at"),
            }
            for r in rows
            # review P2: list only same-team invitations; cross-team ones cannot be accepted (accept also returns 403),
            # so omit phantom entries from onboarding.
            if (r.get("team_id") or "") == (user.team_id or "")
        ]
    }


@router.post(
    "/me/invites/{invite_id}/accept",
    dependencies=[Depends(require_not_readonly)],
)
async def accept_my_invite(
    invite_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict:
    store: UserStore = request.app.state.user_store
    email = (user.email or "").strip()
    if not email:
        raise HTTPException(400, "user has no email")
    if not bool(getattr(user, "email_verified", False)):
        raise HTTPException(403, "email not verified — 请先验证邮箱再接受邀请")
    rows = await store.list_active_invites_for_email(email)
    row = next((r for r in rows if r["id"] == invite_id), None)
    if row is None:
        raise HTTPException(404, "invite not found or expired")
    # review P2: cross-team invite (team B admin invited team A user's email) cannot be accepted: single-team/user
    # model's add_member raises ValueError for team mismatch. Explicit 403 here prevents reaching it below.
    if (row.get("team_id") or "") != (user.team_id or ""):
        raise HTTPException(403, "invite belongs to a different team")
    # review P2: add_member before consume (same safe order as registration), so failed membership creation leaves
    # the invite active/retryable. Reversed order permanently consumes it as an orphan (no member, irrecoverable invalid link).
    try:
        await store.add_member(
            workspace_id=row["workspace_id"],
            user_id=user.id,
            role=row.get("role") or "member",
        )
    except SpaceGovernanceError as exc:
        raise HTTPException(409, {"code": exc.code}) from exc
    consumed = await store.consume_invite_by_id(invite_id)
    if not consumed:
        # Concurrent acceptance: member already created (idempotent INSERT OR REPLACE in add_member), invite consumed
        # by another request; benign, continue.
        log.info("invite.accept_race invite=%s ws=%s", invite_id, row["workspace_id"])
    await store.log_audit(
        user_id=user.id,
        team_id=user.team_id,
        event_type="workspace_invite_accepted",
        ip=request.client.host if request.client else None,
        payload={"invite_id": invite_id, "workspace_id": row["workspace_id"]},
    )
    return {"ok": True, "workspace_id": row["workspace_id"]}

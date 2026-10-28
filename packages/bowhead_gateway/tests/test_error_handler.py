"""i18n v1 · FastAPI exception handler 单测.

验证 SkillUploadError(code=...) → 异常处理器 → 按 user ui_language 渲染本地化 4xx body.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx2
import jwt as _jwt
import pytest
from bowhead_core.error_catalog import (
    default_error_catalog_dir,
    load_error_catalog,
)
from bowhead_core.skill_upload import SkillUploadError
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_gateway._entitlement_gate import assert_feature_licensed
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes.auth import _validate_password_strength
from bowhead_gateway.rule_clarification_errors import (
    rule_clarification_http_error,
)
from bowhead_gateway.skill_workflow_errors import skill_workflow_http_error
from fastapi import APIRouter, FastAPI, HTTPException, Request

if TYPE_CHECKING:
    from pathlib import Path

_JWT_SECRET = "test-secret-long-enough-for-hs256-error-handler"


def _jwt_cookie(user_id: str) -> str:
    """A real signed session token — the identity the error handler must read."""
    return _jwt.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600},
        _JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
async def app_with_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """构造一个最小 FastAPI app，挂 i18n 异常处理器 + error_catalog + user_settings_store."""
    # Prod-shaped auth: a real signed JWT is the only accepted identity, and the
    # dev_default_user escape hatch is OFF (it is OFF in every real deployment).
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    app = FastAPI()
    register_error_handlers(app)

    # Load the real error_catalog.
    catalog_dir = default_error_catalog_dir()
    if not catalog_dir.exists():
        pytest.skip(f"error catalog not found: {catalog_dir}")
    catalog = load_error_catalog(catalog_dir)

    # mock user_settings_store
    uss = UserSettingsStore(tmp_path / "us.db")
    await uss.init()

    # Mock settings (only dev_default_user needed).
    class _Settings:
        dev_default_user = "u_test"

    app.state.settings = _Settings()
    app.state.error_catalog = catalog
    app.state.user_settings_store = uss

    # Routes: query triggers raises, both with and without a code.
    router = APIRouter()

    @router.post("/raise/coded")
    async def raise_coded(code: str = "E_SKILL_MISSING_MANIFEST"):
        if code == "E_SKILL_INVALID_ZIP_PATH":
            raise SkillUploadError(code=code, path="../../../etc/passwd")
        raise SkillUploadError(code=code)

    @router.post("/raise/legacy")
    async def raise_legacy():
        # Legacy path: direct message string, without code.
        raise SkillUploadError("legacy 中文 message")

    @router.post("/raise/http-coded")
    async def raise_http_coded(code: str = "not_local_deployment"):
        raise HTTPException(status_code=409, detail={"code": code})

    @router.post("/raise/http-message")
    async def raise_http_message():
        raise HTTPException(
            status_code=409,
            detail={"code": "not_local_deployment", "message": "explicit message"},
        )

    @router.post("/raise/http-nested")
    async def raise_http_nested(code: str = "public_base_url_changed"):
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": code}},
        )

    @router.post("/raise/http-skill")
    async def raise_http_skill():
        raise skill_workflow_http_error(404, "skill_not_found")

    @router.post("/raise/http-skill-params")
    async def raise_http_skill_params():
        raise skill_workflow_http_error(
            422,
            "E_SKILL_TOO_LARGE",
            max_mb=5,
            path="/srv/private/skill.zip",
            upstream_message="RAW UPSTREAM FAILURE",
        )

    @router.post("/raise/http-skill-unknown")
    async def raise_http_skill_unknown():
        raise skill_workflow_http_error(
            422,
            "E_NEVER_REGISTERED",
            upstream_message="RAW UPSTREAM FAILURE",
        )

    @router.post("/raise/http-clarification")
    async def raise_http_clarification(code: str = "clarification_input_rejected", status: int = 422):
        raise rule_clarification_http_error(
            status,
            code,
        )

    @router.post("/raise/http-resume-preflight")
    async def raise_http_resume_preflight():
        raise rule_clarification_http_error(
            409, "clarification_resume_unavailable", nested_error=True, clarification_stage="clarification_resume_preflight"
        )

    @router.post("/raise/feature-gate")
    async def raise_feature_gate(request: Request):
        # The REAL licensing gate, not a copy of its payload: these are the codes an
        # unlicensed enterprise deployment actually serves.
        await assert_feature_licensed(request, "mcp")

    app.include_router(router)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("zh-CN", "安全或格式要求"),
        ("en-US", "safety or format requirements"),
    ],
)
async def test_rule_clarification_error_is_localized_and_published(
    app_with_handler: FastAPI,
    language: str,
    expected: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post("/raise/http-clarification")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "clarification_input_rejected"
    assert expected in detail["message"]
    assert detail["diagnostics"]["diagnostic_code"] == ("rule_clarification_failure")


@pytest.mark.asyncio
@pytest.mark.parametrize(("language", "expected"), [("zh-CN", "请保留当前回答"), ("en-US", "Keep your answer")])
async def test_pending_pause_error_preserves_the_answer_without_claiming_it_was_saved(
    app_with_handler: FastAPI, language: str, expected: str
) -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app_with_handler), base_url="http://gw", cookies={"NEXT_LOCALE": language}
    ) as client:
        response = await client.post("/raise/http-clarification?code=clarification_pause_pending&status=409")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "clarification_pause_pending"
    assert expected in detail["message"]
    assert detail["diagnostics"]["retryable"] is True


@pytest.mark.asyncio
async def test_coded_error_renders_english_by_default(app_with_handler: FastAPI) -> None:
    """Missing language preferences use English."""
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/raise/coded?code=E_SKILL_MISSING_MANIFEST")
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == "E_SKILL_MISSING_MANIFEST"
        assert "missing" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_coded_error_renders_en_via_accept_language(
    app_with_handler: FastAPI,
) -> None:
    """Accept-Language: en-US → 英文 message."""
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        headers={"accept-language": "en-US,en;q=0.9"},
    ) as c:
        r = await c.post("/raise/coded?code=E_SKILL_MISSING_MANIFEST")
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == "E_SKILL_MISSING_MANIFEST"
        assert "missing" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    ("ui_language", "browser_language", "expected"),
    [
        ("en-US", "zh-CN", "missing"),  # English account, Chinese browser → English
        ("zh-CN", "en-US", "缺少"),  # Chinese account, English browser → Chinese
    ],
)
@pytest.mark.asyncio
async def test_signed_in_user_ui_language_overrides_accept_language(
    app_with_handler: FastAPI,
    ui_language: str,
    browser_language: str,
    expected: str,
) -> None:
    """账号里存着的 ui_language 覆盖 Accept-Language —— 身份来自真 JWT，不是 dev_default_user.

    两个方向都测：只断言一边时，一个「永远返回 en-US」的实现也会绿。
    """
    uss: UserSettingsStore = app_with_handler.state.user_settings_store
    await uss.set("u_real", "ui_language", ui_language)

    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        headers={"accept-language": browser_language},
        cookies={"bowhead_jwt": _jwt_cookie("u_real")},
    ) as c:
        r = await c.post("/raise/coded?code=E_SKILL_MISSING_MANIFEST")

    assert r.status_code == 400
    assert expected in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_error_language_reads_the_caller_not_some_other_user(
    app_with_handler: FastAPI,
) -> None:
    """语言按**请求里那个** subject 取；别人的偏好不得泄漏到本次响应。"""
    uss: UserSettingsStore = app_with_handler.state.user_settings_store
    await uss.set("u_english", "ui_language", "en-US")

    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        headers={"accept-language": "zh-CN"},
        cookies={"bowhead_jwt": _jwt_cookie("u_without_setting")},
    ) as c:
        r = await c.post("/raise/coded?code=E_SKILL_MISSING_MANIFEST")

    assert "缺少" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_unauthenticated_request_ignores_dev_default_user(
    app_with_handler: FastAPI,
) -> None:
    """Anonymous requests follow the browser instead of another account's setting."""
    uss: UserSettingsStore = app_with_handler.state.user_settings_store
    await uss.set("u_test", "ui_language", "en-US")  # dev_default_user's preference.

    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        headers={"accept-language": "zh-CN"},
    ) as c:
        r = await c.post("/raise/coded?code=E_SKILL_MISSING_MANIFEST")

    assert r.status_code == 400
    assert "缺少" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_unparsable_session_cookie_still_returns_the_real_error(
    app_with_handler: FastAPI,
) -> None:
    """取语言时的认证失败不得把用户的真错误换成 500。"""
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        headers={"accept-language": "en-US"},
        cookies={"bowhead_jwt": "not-a-jwt"},
    ) as c:
        r = await c.post("/raise/coded?code=E_SKILL_MISSING_MANIFEST")

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_SKILL_MISSING_MANIFEST"
    assert "missing" in r.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_user_settings_failure_still_returns_the_real_error(
    app_with_handler: FastAPI,
) -> None:
    """user_settings 读失败 → 降级到 Accept-Language，而不是 500。"""

    class _BrokenSettingsStore:
        async def get(self, user_id: str, key: str) -> str | None:
            raise RuntimeError("user_settings unavailable")

    app_with_handler.state.user_settings_store = _BrokenSettingsStore()

    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        headers={"accept-language": "en-US"},
        cookies={"bowhead_jwt": _jwt_cookie("u_real")},
    ) as c:
        r = await c.post("/raise/coded?code=E_SKILL_MISSING_MANIFEST")

    assert r.status_code == 400
    assert "missing" in r.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_browser_locale_cookie_still_wins_over_account_setting(
    app_with_handler: FastAPI,
) -> None:
    """解析顺序不变：NEXT_LOCALE（当前浏览器）仍排在账号设置之前。"""
    uss: UserSettingsStore = app_with_handler.state.user_settings_store
    await uss.set("u_real", "ui_language", "en-US")

    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"bowhead_jwt": _jwt_cookie("u_real"), "NEXT_LOCALE": "zh-CN"},
    ) as c:
        r = await c.post("/raise/coded?code=E_SKILL_MISSING_MANIFEST")

    assert "缺少" in r.json()["error"]["message"]


# ─── A0-2: Hardcoded message overrides its own translation ──────────────────


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en-US", "license status cannot be verified"),
        ("zh-CN", "暂时无法确认许可证状态"),
    ],
)
@pytest.mark.asyncio
async def test_entitlement_unavailable_is_localized(
    app_with_handler: FastAPI,
    language: str,
    expected: str,
) -> None:
    """503 许可子系统未就绪：曾带中文 message，英文会话也只能看到中文。"""
    app_with_handler.state.deployment_mode = "local"  # INV-1: local + no resolver = deny.

    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as c:
        r = await c.post("/raise/feature-gate")

    assert r.status_code == 503
    error = r.json()["detail"]["error"]
    assert error["code"] == "entitlement_unavailable"
    assert expected in error["message"]


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en-US", "has not completed license activation"),
        ("zh-CN", "尚未完成授权激活"),
    ],
)
@pytest.mark.asyncio
async def test_needs_license_is_localized(
    app_with_handler: FastAPI,
    language: str,
    expected: str,
) -> None:
    """403 未激活：同上。无会话 → team 解析不出 → needs_license。"""
    app_with_handler.state.deployment_mode = "local"
    app_with_handler.state.entitlements = object()  # Resolver exists, but caller lacks a team.

    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as c:
        r = await c.post("/raise/feature-gate")

    assert r.status_code == 403
    error = r.json()["detail"]["error"]
    assert error["code"] == "needs_license"
    assert expected in error["message"]


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en-US", "does not include this feature"),
        ("zh-CN", "未包含此功能"),
    ],
)
@pytest.mark.asyncio
async def test_feature_unlicensed_is_localized_and_keeps_its_details(
    app_with_handler: FastAPI,
    language: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 功能未授权：``details`` 这个机器契约保留，人读的那句改由 catalog 出。"""

    class _NoMcpResolver:
        async def is_feature_licensed(self, team_id: str | None, feature: str) -> bool:
            return False

    async def _team(request: Request, ent: object) -> str:
        return "team_x"

    monkeypatch.setattr("bowhead_gateway._entitlement_gate._resolve_team_id", _team)
    app_with_handler.state.deployment_mode = "local"
    app_with_handler.state.entitlements = _NoMcpResolver()

    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as c:
        r = await c.post("/raise/feature-gate")

    assert r.status_code == 403
    error = r.json()["detail"]["error"]
    assert error["code"] == "feature_unlicensed"
    assert error["details"] == {"feature": "mcp"}
    assert expected in error["message"]


@pytest.mark.asyncio
async def test_coded_error_with_params(app_with_handler: FastAPI) -> None:
    """不安全路径参数不得进入用户消息或技术详情."""
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/raise/coded?code=E_SKILL_INVALID_ZIP_PATH")
        body = r.json()
        assert body["error"]["code"] == "E_SKILL_INVALID_ZIP_PATH"
        assert "../../../etc/passwd" not in str(body)
        assert body["error"]["diagnostics"]["diagnostic_code"] == ("skill_workflow_failure")


@pytest.mark.asyncio
async def test_legacy_message_fails_closed(app_with_handler: FastAPI) -> None:
    """无 code 的老 raise → 登记的通用 skill 错误，原文不出响应."""
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/raise/legacy")
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == "skill_bundle_invalid"
        assert "legacy 中文 message" not in str(body)
        assert body["error"]["diagnostics"]["diagnostic_code"] == ("skill_workflow_failure")


@pytest.mark.asyncio
async def test_unknown_code_fails_closed(
    app_with_handler: FastAPI,
) -> None:
    """未登记 code 不得穿过有限错误边界."""
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/raise/coded?code=E_NEVER_REGISTERED")
        body = r.json()
        assert body["error"]["code"] == "skill_bundle_invalid"
        assert "E_NEVER_REGISTERED" not in str(body)


@pytest.mark.asyncio
async def test_registered_http_exception_gets_localized_message(
    app_with_handler: FastAPI,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post("/raise/http-coded")

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "not_local_deployment",
            "message": "This action is only available in private deployments.",
        },
    }


@pytest.mark.asyncio
async def test_skill_workflow_http_error_is_localized_and_published(
    app_with_handler: FastAPI,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": "en-US"},
    ) as client:
        response = await client.post("/raise/http-skill")

    detail = response.json()["detail"]
    assert response.status_code == 404
    assert detail["code"] == "skill_not_found"
    assert detail["message"] == ("This skill could not be found, or you no longer have access. Refresh the list.")
    assert detail["diagnostics"]["diagnostic_code"] == ("skill_workflow_failure")


@pytest.mark.asyncio
async def test_skill_workflow_http_error_keeps_only_registered_params(
    app_with_handler: FastAPI,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post("/raise/http-skill-params")

    detail = response.json()["detail"]
    assert response.status_code == 422
    assert detail["code"] == "E_SKILL_TOO_LARGE"
    assert detail["max_mb"] == 5
    assert "5 MB" in detail["message"]
    assert "/srv/private/skill.zip" not in str(detail)
    assert "RAW UPSTREAM FAILURE" not in str(detail)


@pytest.mark.asyncio
async def test_skill_workflow_http_error_unknown_code_fails_closed(
    app_with_handler: FastAPI,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post("/raise/http-skill-unknown")

    detail = response.json()["detail"]
    assert response.status_code == 422
    assert detail["code"] == "skill_bundle_invalid"
    assert "E_NEVER_REGISTERED" not in str(detail)
    assert "RAW UPSTREAM FAILURE" not in str(detail)


@pytest.mark.parametrize(
    "code",
    [
        "not_local_deployment",
        "pubkeys_unavailable",
        "email_already_registered",
        "invalid_public_base_url",
        "public_base_url_changed",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_activation_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as c:
        response = await c.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "not_local_deployment",
        "pubkeys_unavailable",
        "managed_by_license",
        "cross_team_not_allowed",
        "stale_license",
        "tamper",
        "team_not_found",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_team_license_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "rate_limited",
        "rate_limit",
        "invite_expired",
        "invite_invalid",
        "invite_revoked",
        "invite_consumed",
        "policy_blocked",
        "trusted_idp_required",
        "identity_already_exists",
        "identity_blocked",
        "verification_delivery_failed",
        "public_entry_team_mismatch",
        "feature_disabled",
        "invite_intent_missing",
        "invite_email_mismatch",
        "invite_cross_team_not_allowed",
        "invite_accept_failed",
        "auth_required",
        "invalid_token",
        "user_inactive",
        "password_changed_relogin_required",
        "email_not_verified",
        "password_length_invalid",
        "password_letter_required",
        "password_digit_required",
        "password_too_common",
        "json_content_type_required",
        "cross_origin_mutation_rejected",
        "same_origin_required",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_invite_flow_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "self_recovery_not_allowed",
        "member_not_found",
        "shared_identity_recovery_requires_self_service",
        "owner_recovery_requires_owner",
        "credential_recovery_not_allowed",
        "recovery_invalid_or_expired",
        "recovery_intent_missing",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_credential_recovery_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "invalid_oauth_flow",
        "oauth_provider_unsupported",
        "oauth_not_configured",
        "oauth_state_invalid",
        "oauth_authorization_incomplete",
        "oauth_token_exchange_failed",
        "oauth_userinfo_failed",
        "oauth_identity_missing",
        "provider_email_untrusted",
        "federated_identity_conflict",
        "invite_activation_failed",
        "oauth_registration_disabled",
        "whitelist_domain_no_team_provisioned",
        "oauth_callback_failed",
        "seat_exceeded",
        "over_grace",
        "suspended",
        "needs_license",
        "tamper",
        "clock_tamper",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_oauth_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "admin_direct_password_disabled",
        "local_only",
        "team_not_found",
        "member_create_conflict",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_direct_member_creation_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "feature_disabled_use_direct_create",
        "seat_full",
        "user_not_found",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_team_invite_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "resend_cooldown_1min",
        "invite_not_found",
        "invite_status_revoked",
        "invite_status_consumed",
        "invite_status_satisfied",
        "invite_status_expired",
        "invite_not_reissuable",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_team_invite_lifecycle_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "team_membership_required",
        "user_not_active",
        "insufficient_role",
        "owner_immutable",
        "last_team_admin_cannot_self_demote",
        "target_user_not_found",
        "cross_team_target_not_allowed",
        "feature_unlicensed",
        "entitlement_unavailable",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_team_role_change_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "insufficient_credits",
        "kb_quota_exceeded",
        "matrix_active_runs_cap",
        "ws_active_runs_cap",
        "no_pending_cells",
        "run_not_cancellable",
        "invalid_matrix_run_scope",
        "matrix_service_unavailable",
        "matrix_not_found",
        "matrix_column_not_found",
        "matrix_file_not_found",
        "matrix_run_not_found",
        "access_control_unavailable",
        "workspace_required",
        "workspace_access_denied",
        "workspace_role_unknown",
        "workspace_member_role_required",
        "owner_or_admin_required",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_kb_matrix_run_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "matrix_data_service_unavailable",
        "matrix_source_file_unavailable",
        "personal_files_not_allowed",
        "invalid_column_type",
        "invalid_matrix_request",
        "matrix_template_not_found",
        "template_use_new_matrix_flow",
        "options_required",
        "option_empty",
        "option_too_long",
        "option_duplicate",
        "options_too_many",
        "options_not_applicable",
        "cross_verify_not_applicable",
        "promote_must_be_sole_change",
        "invalid_promotion",
        "too_many_columns_to_graduate",
        "empty_matrix",
        "duplicate_column_names",
        "invalid_column_name",
        "column_name_too_long",
        "too_many_files_to_graduate",
        "graduation_rows_failed",
        "doc_type_name_taken",
        "invalid_graduation_request",
        "matrix_cell_not_found",
        "cell_value_too_large",
        "cell_value_not_serializable",
        "not_edited",
        "verified_required",
        "verify_requires_done",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_kb_matrix_mutation_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "kb_doc_type_service_unavailable",
        "kb_doc_type_write_unavailable",
        "doc_type_not_found",
        "doc_type_name_taken",
        "graduated_fields_frozen",
        "too_many_doc_type_fields",
        "duplicate_field_names",
        "invalid_doc_type_schema",
        "doc_type_draft_service_unavailable",
        "doc_type_draft_failed",
        "doc_type_draft_invalid_response",
        "workspace_admin_or_creator_required",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_kb_doc_type_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "team_template_not_found",
        "team_template_service_unavailable",
        "team_template_copy_refresh_required",
        "not_publishable",
        "empty_template",
        "schema_too_large",
        "invalid_template_schema",
        "revoke_not_allowed",
        "copy_into_source_space",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_kb_team_template_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "owner_transfer_same_user",
        "owner_transfer_target_not_active",
        "owner_transfer_conflict",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_owner_transfer_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "workspace_not_found",
        "last_workspace_admin",
        "workspace_team_mismatch",
        "grant_not_found",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_member_workspace_grant_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "cannot_self_deactivate",
        "last_team_admin",
        "already_inactive",
        "subagent_lifecycle_action_required",
        "deactivate_failed",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_member_deactivation_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "member_service_unavailable",
        "not_workspace_member",
        "too_many_checks",
        "workspace_member_not_found",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_space_member_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "provider_not_configured",
        "config_decrypt_failed",
        "config_version_conflict",
        "invalid_llm_config",
        "embed_reindex_required",
        "llm_config_encryption_required",
        "provider_not_in_v2_config",
        "reindex_task_not_found",
        "reindex_failed",
        "provider_not_testable",
        "provider_type_unsupported",
        "invalid_schema",
        "blocked_url",
        "unknown_reference",
        "required",
        "unsupported",
        "invalid_value",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_admin_billing_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "insufficient_credit_capability",
        "invalid_credit_pagination",
        "credit_changes_required",
        "credit_user_inactive",
        "credit_team_pool_insufficient",
        "credit_period_reset_cooldown",
        "credit_attention_not_found",
        "credit_attention_reason_required",
        "credit_user_id_required",
        "email_config_encryption_required",
        "invalid_smtp_config",
        "smtp_control_characters",
        "smtp_host_required",
        "smtp_host_invalid",
        "smtp_port_unsupported",
        "smtp_from_address_invalid",
        "smtp_password_required",
        "smtp_host_blocked",
        "email_not_configured",
        "email_test_delivery_failed",
        "email_test_env_delivery_failed",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_admin_credit_email_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-coded?code={code}")

    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["message"] != code


@pytest.mark.parametrize(
    "code",
    [
        "missing_workspace_context",
        "workspace_admin_required",
        "resource_grant_denied",
        "kb_scope_access_denied",
        "kb_file_access_denied",
        "kb_file_not_found",
        "kb_artifact_not_found",
        "kb_file_not_in_knowledge_base",
        "kb_service_unavailable",
        "kb_doc_type_service_unavailable",
        "kb_file_write_failed",
        "kb_file_operation_failed",
        "kb_file_too_large",
        "kb_file_type_unsupported",
        "kb_bundle_invalid",
        "kb_bundle_required",
        "kb_bundle_too_large",
        "kb_bundle_uncompressed_too_large",
        "kb_bundle_too_many_entries",
        "kb_promotion_not_supported",
        "kb_scope_change_reason_required",
        "kb_scope_change_failed",
        "invalid_kb_bulk_request",
        "invalid_kb_pagination",
        "kb_import_path_forbidden",
        "kb_import_file_not_found",
        "doc_type_not_found",
        "doc_type_not_bindable",
        "file_has_no_doc_type",
        "doc_type_extract_disabled",
        "per_row_extract_unavailable",
        "rate_limit_per_file",
        "rate_limit_per_ws_daily",
        "cross_ws_backfill_forbidden",
        "cross_ws_backfill_status_forbidden",
        "cross_ws_backfill_cancel_forbidden",
        "backfill_already_running",
        "backfill_task_not_found",
        "kb_not_ready",
        "kb_backfill_failed",
    ],
)
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.asyncio
async def test_all_kb_file_errors_have_localized_messages(
    app_with_handler: FastAPI,
    code: str,
    language: str,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": language},
    ) as client:
        response = await client.post(f"/raise/http-nested?code={code}")

    error = response.json()["detail"]["error"]
    assert error["code"] == code
    assert error["message"]
    assert error["message"] != code


@pytest.mark.asyncio
async def test_next_locale_cookie_selects_http_exception_language(
    app_with_handler: FastAPI,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        cookies={"NEXT_LOCALE": "en-US"},
    ) as c:
        response = await c.post("/raise/http-coded")

    assert response.json()["detail"]["message"] == ("This action is only available in private deployments.")


@pytest.mark.asyncio
async def test_http_exception_preserves_explicit_message(
    app_with_handler: FastAPI,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post("/raise/http-message")

    assert response.json()["detail"]["message"] == "explicit message"


@pytest.mark.asyncio
async def test_unknown_http_exception_code_is_not_changed(
    app_with_handler: FastAPI,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post("/raise/http-coded?code=not_registered")

    assert response.json() == {"detail": {"code": "not_registered"}}


@pytest.mark.asyncio
async def test_nested_http_exception_error_gets_localized_message(
    app_with_handler: FastAPI,
) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post("/raise/http-nested")

    assert response.json() == {
        "detail": {
            "error": {
                "code": "public_base_url_changed",
                "message": "The public entry has changed. Refresh the page and confirm it again.",
            },
        },
    }


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("short1", "password_length_invalid"),
        ("12345678", "password_letter_required"),
        ("abcdefgh", "password_digit_required"),
        ("password1", "password_too_common"),
    ],
)
def test_password_policy_raises_structured_codes(
    password: str,
    code: str,
) -> None:
    with pytest.raises(HTTPException) as caught:
        _validate_password_strength(password)

    assert caught.value.status_code == 422
    assert caught.value.detail == {"code": code}


@pytest.mark.asyncio
async def test_client_dropping_its_upload_is_not_reported_as_a_server_error(
    app_with_handler: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """浏览器中止在途请求（输入框每敲一键都会取消上一条目录查询）不是服务端故障。

    自读 ``request.stream()`` 的路由在 body 传到一半收到 ``http.disconnect`` 会抛
    ``ClientDisconnect``；它此前落进最终 500 处理器，每次都打一条 traceback。
    """

    @app_with_handler.post("/reads-own-body")
    async def _reads_own_body(request: Request) -> dict[str, int]:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
        return {"size": size}

    events = iter(
        [
            {"type": "http.request", "body": b'{"query":"@da', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    sent: list[dict] = []

    async def receive() -> dict:
        return next(events)

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/reads-own-body",
        "raw_path": b"/reads-own-body",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("gw", 80),
    }
    with caplog.at_level("INFO", logger="bowhead_gateway.error_handler"):
        await app_with_handler(scope, receive, send)

    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [499]
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("gateway.client_disconnected") for m in messages)
    assert not any(m.startswith("gateway.unhandled_exception") for m in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en-US", "checks before continuing"),
        ("zh-CN", "继续前的检查"),
        ("de-DE", "Prüfungen vor der Fortsetzung"),
        ("fr-FR", "vérifications avant la reprise"),
    ],
)
async def test_native_resume_preflight_explanation_preserves_wire_identity(app_with_handler: FastAPI, language: str, expected: str) -> None:
    transport = httpx2.ASGITransport(app=app_with_handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw", cookies={"NEXT_LOCALE": language}) as client:
        response = await client.post("/raise/http-resume-preflight")
    assert response.status_code == 409
    error = response.json()["detail"]["error"]
    assert error["code"] == "clarification_resume_unavailable"
    assert expected in error["message"]
    assert error["diagnostics"]["safe_detail"] == "phase=clarification_resume_preflight; failure_class=state"
    assert error["diagnostics"]["retryable"] is False

"""账号里存着的 UI 语言必须能被读回来。

2026-08-19 在 116 实测到的形状：`user_settings.ui_language`、`output_language`、
`users.locale` **三处全是 `zh-CN`**，页面却渲染 en-US，顶栏还宣称 current: English。
根因是语言偏好**写了三处、读零处** —— 决定 UI 语言的两条读路径（网关选哪份静态构建、
前端 `detectLocale()`）都只看 `NEXT_LOCALE` cookie，cookie 没有就跟浏览器语言走。
换设备、清 cookie、无痕窗口时，设过的语言等于没设过。

这里钉的是那条把「存下来的设置」接回 cookie 的同步：cookie 仍是唯一传输载体，
不新增第三条判据。
"""

from __future__ import annotations

import time

import aiosqlite
import httpx2
import jwt
import pytest
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore, _id, _now, hash_password
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

_SECRET = "test_secret_auth_locale_cookie"
_PASSWORD = "Pa55w0rd!locale"


@pytest.fixture
async def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.delenv("BOWHEAD_PROD", raising=False)
    db = tmp_path / "auth_locale_cookie.db"
    user_store = UserStore(str(db))
    await user_store.initialize()
    settings_store = UserSettingsStore(str(db))
    await settings_store.init()
    app = create_app()
    app.state.settings = Settings()
    app.state.user_store = user_store
    app.state.user_settings_store = settings_store
    return app, user_store, settings_store


async def _seed(store: UserStore, *, user_id: str = "u_locale", team_id: str = "team_locale") -> None:
    now = _now()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, "Locale Team", "locale.test", 10, "ultra", 100_000, 100_000, '{"ultra":30000}', "active", now),
        )
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES (?, ?, ?, 1, ?, '[]', 'ultra', 30000, 30000, ?, 'zh-CN', "
            "'UTC', '{}', 'active', ?)",
            (user_id, team_id, f"{user_id}@locale.test", hash_password(_PASSWORD), now, now),
        )
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, promoted_by, promoted_at, status, created_at) "
            "VALUES (?, ?, ?, 'team_admin', 'u_creator', ?, 'active', ?)",
            (_id("om"), user_id, team_id, now, now),
        )
        await db.commit()


def _jwt(user_id: str = "u_locale", team_id: str = "team_locale") -> str:
    return jwt.encode(
        {"sub": user_id, "team_id": team_id, "iat": int(time.time()), "exp": int(time.time()) + 3600},
        _SECRET,
        algorithm="HS256",
    )


async def _me(app, *, cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        client.cookies.set("bowhead_jwt", _jwt())
        for name, value in (cookies or {}).items():
            client.cookies.set(name, value)
        return await client.get("/api/v1/auth/me", headers=headers or {})


@pytest.mark.integration
async def test_me_seeds_locale_cookie_from_account_setting(app_ctx) -> None:
    """没有 cookie 的会话（换了浏览器）拿回账号里设过的语言。"""

    app, store, settings = app_ctx
    await _seed(store)
    await settings.set("u_locale", "ui_language", "zh-CN")

    response = await _me(app)

    assert response.status_code == 200, response.text
    assert response.cookies.get("NEXT_LOCALE") == "zh-CN"


@pytest.mark.integration
async def test_account_setting_beats_browser_language(app_ctx) -> None:
    """★ 事故原样：浏览器说英文、账号说中文 —— 以账号为准。

    这正是 116 上看到的那一幕（企业统装的机器普遍报 en-US）。
    """

    app, store, settings = app_ctx
    await _seed(store)
    await settings.set("u_locale", "ui_language", "zh-CN")

    response = await _me(app, headers={"accept-language": "en-US,en;q=0.9"})

    assert response.cookies.get("NEXT_LOCALE") == "zh-CN"


@pytest.mark.integration
async def test_cookie_is_readable_by_the_client(app_ctx) -> None:
    """必须非 httpOnly：前端 `detectLocale()` 用 `document.cookie` 读它。

    它只是展示偏好，不携带凭据 —— 设成 httpOnly 会让这条同步在前端那一侧彻底失效
    （网关那侧仍生效，于是变成「首屏对、切页错」的更难查的形态）。
    """

    app, store, settings = app_ctx
    await _seed(store)
    await settings.set("u_locale", "ui_language", "en-US")

    response = await _me(app)

    raw = [value for key, value in response.headers.items() if key.lower() == "set-cookie" and "NEXT_LOCALE" in value]
    assert raw, response.headers
    assert "httponly" not in raw[0].lower()


@pytest.mark.integration
async def test_no_setting_means_no_opinion(app_ctx) -> None:
    """负对照：没设过就不发这个 cookie（不替用户猜，浏览器语言仍然兜底）。"""

    app, store, _settings = app_ctx
    await _seed(store)

    response = await _me(app)

    assert response.cookies.get("NEXT_LOCALE") is None


@pytest.mark.integration
async def test_unsupported_stored_value_is_ignored(app_ctx) -> None:
    """负对照：库里是脏值时当没设，不把它塞进 cookie 让下游去猜。"""

    app, store, settings = app_ctx
    await _seed(store)
    await settings.set("u_locale", "ui_language", "ja-JP")

    response = await _me(app)

    assert response.cookies.get("NEXT_LOCALE") is None


@pytest.mark.integration
async def test_stored_french_is_synced_to_the_cookie(app_ctx) -> None:
    """法语是受支持的界面语言：账号里存的 fr-FR 要同步成 cookie（换设备也生效）。"""

    app, store, settings = app_ctx
    await _seed(store)
    await settings.set("u_locale", "ui_language", "fr-FR")

    response = await _me(app, headers={"accept-language": "zh-CN"})

    assert response.cookies.get("NEXT_LOCALE") == "fr-FR"


@pytest.mark.integration
async def test_already_consistent_does_not_resend(app_ctx) -> None:
    """负对照：cookie 已经一致时不重发 —— `/me` 每次开页都调，别每次都写一遍。"""

    app, store, settings = app_ctx
    await _seed(store)
    await settings.set("u_locale", "ui_language", "zh-CN")

    response = await _me(app, cookies={"NEXT_LOCALE": "zh-CN"})

    assert not [v for k, v in response.headers.items() if k.lower() == "set-cookie" and "NEXT_LOCALE" in v]


@pytest.mark.integration
async def test_login_carries_the_setting_to_a_new_device(app_ctx) -> None:
    """新设备第一次登录就该拿到语言，而不是先渲一屏错语言再纠正。"""

    app, store, settings = app_ctx
    await _seed(store)
    await settings.set("u_locale", "ui_language", "zh-CN")

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "u_locale@locale.test", "password": _PASSWORD},
            headers={"accept-language": "en-US,en;q=0.9"},
        )

    assert response.status_code == 200, response.text
    assert response.cookies.get("NEXT_LOCALE") == "zh-CN"


@pytest.mark.integration
async def test_automatic_preference_clears_a_previous_language_cookie(app_ctx) -> None:
    app, store, settings = app_ctx
    await _seed(store)
    await settings.set("u_locale", "ui_language", "auto")
    response = await _me(app, cookies={"NEXT_LOCALE": "en-US"}, headers={"accept-language": "zh-CN"})
    cookie_headers = response.headers.get_list("set-cookie")
    assert any("NEXT_LOCALE=" in value and "Max-Age=0" in value for value in cookie_headers)

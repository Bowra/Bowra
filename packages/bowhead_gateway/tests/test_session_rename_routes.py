"""Gateway contract for renaming a session from the session row menu."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.session_title import generate_and_store_title
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.sessions import SESSION_TITLE_MAX_CHARS
from bowhead_gateway.settings import Settings

from bowhead_core import SessionStore

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


@pytest.fixture
async def app_ctx(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    await store.initialize()
    app = create_app()
    app.state.settings = Settings()
    app.state.session_store = store
    return app


def _client(app) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw")


def _error_code(response: httpx2.Response) -> str:
    return str(response.json()["detail"]["error"]["code"])


def _listed_titles(body: dict) -> dict[str, str]:
    return {item["id"]: item["title"] for group in body["groups"] for item in group["items"]}


class _TitleRouter:
    """Stands in for the team router: always proposes the same automatic title."""

    def __init__(self, title: str) -> None:
        self.title = title

    async def invoke(self, agent_type, request):
        return SimpleNamespace(content=self.title)


async def test_rename_trims_persists_and_is_reflected_in_list_and_detail(app_ctx) -> None:
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    session = await store.ensure_session(session_id="s_rename", workspace_id="w1", user_id=uid)
    await store.set_title(session_id=session.id, title="Automatic title")
    before = await store.get_session(session.id)

    async with _client(app_ctx) as client:
        response = await client.patch(f"/api/v1/sessions/{session.id}", json={"title": "  Q3 revenue review  "})
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == session.id
        assert body["title"] == "Q3 revenue review"
        # Renaming does not reorder the list.
        assert body["last_active_at"] == before.last_active_at

        listed = _listed_titles((await client.get("/api/v1/sessions")).json())
        assert listed[session.id] == "Q3 revenue review"
        detail = (await client.get(f"/api/v1/sessions/{session.id}")).json()
        assert detail["session"]["title"] == "Q3 revenue review"


@pytest.mark.parametrize(
    ("title", "code"),
    [
        ("", "session_title_empty"),
        ("   　 ", "session_title_empty"),
        ("x" * (SESSION_TITLE_MAX_CHARS + 1), "session_title_too_long"),
        ("first line\nsecond line", "session_title_invalid"),
        ("tab\tinside", "session_title_invalid"),
        ("nul\x00byte", "session_title_invalid"),
        ("a\u2028b", "session_title_invalid"),
        ("a\u2029b", "session_title_invalid"),
        ("\u202eevil", "session_title_invalid"),
        ("\u2066isolate\u2069", "session_title_invalid"),
        ("zero\u200bwidth", "session_title_invalid"),
        ("\u200b", "session_title_empty"),
        ("\u200d", "session_title_empty"),
        ("\ufe0f\u200c", "session_title_empty"),
        ("a\u200eb", "session_title_invalid"),
        ("a\u061cb", "session_title_invalid"),
        ("word\u2060joiner", "session_title_invalid"),
        ("bom\ufeff", "session_title_invalid"),
        (" \u200b\ufeff ", "session_title_empty"),
    ],
)
async def test_rename_rejects_unusable_titles_and_keeps_the_old_one(app_ctx, title: str, code: str) -> None:
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    session = await store.ensure_session(session_id="s_invalid", workspace_id="w1", user_id=uid)
    await store.set_title(session_id=session.id, title="Kept title")

    async with _client(app_ctx) as client:
        response = await client.patch(f"/api/v1/sessions/{session.id}", json={"title": title})
    assert response.status_code == 422
    assert _error_code(response) == code
    assert (await store.get_session(session.id)).title == "Kept title"


@pytest.mark.parametrize(
    ("title", "language", "expected"),
    [
        ("", "en-US", "The title can't be empty. Enter some visible text."),
        ("x" * (SESSION_TITLE_MAX_CHARS + 1), "zh-CN", "标题不能超过 100 个字符，请缩短后再保存。"),
        ("tab\tinside", "de-DE", "Titel dürfen keine Zeilenumbrüche"),
    ],
)
async def test_rename_rejection_carries_a_localized_message(app_ctx, title: str, language: str, expected: str) -> None:
    from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog

    app_ctx.state.error_catalog = load_error_catalog(default_error_catalog_dir())
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    session = await store.ensure_session(session_id="s_message", workspace_id="w1", user_id=uid)

    async with _client(app_ctx) as client:
        response = await client.patch(f"/api/v1/sessions/{session.id}", json={"title": title}, headers={"Accept-Language": language})
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["message"].startswith(expected)


async def test_rename_accepts_the_maximum_length_and_non_latin_titles(app_ctx) -> None:
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    session = await store.ensure_session(session_id="s_limit", workspace_id="w1", user_id=uid)

    async with _client(app_ctx) as client:
        longest = "长" * SESSION_TITLE_MAX_CHARS
        response = await client.patch(f"/api/v1/sessions/{session.id}", json={"title": longest})
        assert response.status_code == 200
        assert response.json()["title"] == longest
        # ZWJ sequences, variation selectors, flags/keycaps and ZWNJ (Persian) are real text.
        for title in ("Umsatz 📈 Überblick", "Coding 👩\u200d💻", "Family 👨\u200d👩\u200d👧", "Love ❤\ufe0f", "#\ufe0f\u20e3 🇩🇪", "می\u200cخواهم"):
            response = await client.patch(f"/api/v1/sessions/{session.id}", json={"title": title})
            assert response.status_code == 200, title
            assert response.json()["title"] == title
        response = await client.patch(f"/api/v1/sessions/{session.id}", json={"title": "a b"})
        assert response.status_code == 200
        assert response.json()["title"] == "a b"


async def test_rename_conceals_foreign_deleted_and_missing_sessions(app_ctx) -> None:
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    foreign = await store.ensure_session(session_id="s_foreign", workspace_id="w1", user_id="u_foreign")
    await store.set_title(session_id=foreign.id, title="Someone else's work")
    deleted = await store.ensure_session(session_id="s_deleted", workspace_id="w1", user_id=uid)
    await store.soft_delete_session(deleted.id)

    async with _client(app_ctx) as client:
        for session_id in (foreign.id, deleted.id, "s_missing"):
            response = await client.patch(f"/api/v1/sessions/{session_id}", json={"title": "Taken over"})
            assert response.status_code == 404
            assert _error_code(response) == "session_not_found"
    assert (await store.get_session(foreign.id)).title == "Someone else's work"
    assert (await store.get_session(deleted.id)).title != "Taken over"


async def test_automatic_title_never_overwrites_a_renamed_session(app_ctx) -> None:
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    renamed = await store.ensure_session(session_id="s_renamed", workspace_id="w1", user_id=uid)
    untouched = await store.ensure_session(session_id="s_untouched", workspace_id="w1", user_id=uid)

    async with _client(app_ctx) as client:
        response = await client.patch(f"/api/v1/sessions/{renamed.id}", json={"title": "My own name"})
        assert response.status_code == 200

    # A title generation that started before the rename finishes after it.
    router = _TitleRouter("Generated summary title")
    for session_id in (renamed.id, untouched.id):
        await generate_and_store_title(
            router=router,
            session_store=store,
            session_id=session_id,
            user_text="question",
            final_answer="answer",
        )
    assert (await store.get_session(renamed.id)).title == "My own name"
    # Positive control: the same writer still titles a session the owner never renamed.
    assert (await store.get_session(untouched.id)).title == "Generated summary title"
    assert await store.set_title(session_id=renamed.id, title="Late automatic title") is False

    async with _client(app_ctx) as client:
        listed = _listed_titles((await client.get("/api/v1/sessions")).json())
    assert listed[renamed.id] == "My own name"

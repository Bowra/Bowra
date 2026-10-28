"""Sprint pptx-from-template M1.4 — web_search → web_clip assets.

Covers RD §7 security (URL-scheme sanitize, control-char strip, javascript:
neutralized) + persistence as a citeable web_clip asset (trust=unverified,
inject=stub) + fail-soft.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.search_config import SearchModelRegistry, SearchModelSpec
from bowhead_core.web_search import _make_handler, _scheme_ok, _slug, _strip_ctrl


def _team_handler():
    """web_search handler wired to a fake team search resolver（团队化后的调用形状）."""
    spec = SearchModelSpec(
        name="qwen-plus-search",
        capability="web_search",
        provider="aliyun",
        real_id="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-test",
        timeout_sec=25,
        protocol="dashscope_compat",
        ssrf_guard=True,
    )
    reg = SearchModelRegistry(by_name={spec.name: spec}, active={"web_search": spec.name})

    async def _resolve(team_id: str) -> SearchModelRegistry | None:
        return reg

    return _make_handler(_resolve)


def test_scheme_and_strip_helpers() -> None:
    assert _scheme_ok("https://x.com") is True
    assert _scheme_ok("http://x.com") is True
    assert _scheme_ok("javascript:alert(1)") is False
    assert _scheme_ok("data:text/html,<script>") is False
    assert _scheme_ok("") is False
    assert _strip_ctrl("a\x07b\x00c") == "abc"
    assert _slug("Good Src! 报告").startswith("Good_Src")


@pytest.fixture
async def store() -> ArtifactStore:
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "test.db"
    s = ArtifactStore(backend=LocalBackend(root_dir=tmp / "files"), db_path=db_path)
    await s.initialize()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS session_attachments ("
            "session_id TEXT NOT NULL, artifact_id TEXT NOT NULL, "
            "pinned_at TEXT NOT NULL, detached_at TEXT, "
            "PRIMARY KEY (session_id, artifact_id))",
        )
        await db.commit()
    return s


class _FakeResp:
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": "summary [1][2]",
                        "citations": [
                            {"title": "Good\x07Src", "url": "https://ex.com/a", "snippet": "sn\x00ip"},
                            {"title": "Evil", "url": "javascript:alert(1)", "snippet": "x"},
                        ],
                    }
                }
            ]
        }


class _FakeClient:
    def __init__(self, *a, **k) -> None: ...
    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def post(self, *a, **k) -> _FakeResp:
        return _FakeResp()


@pytest.mark.asyncio
async def test_web_clip_persist_and_sanitize(
    store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2

    monkeypatch.setattr(httpx2, "AsyncClient", _FakeClient)

    ctx = SimpleNamespace(
        artifact_store=store,
        user_id="u1",
        session_id="s_web",
        turn_id="t_web",
        canvas_id=None,
        team_id="team_1",
    )
    out = await _team_handler()({"query": "行业均值"}, ctx)
    results = out["results"]
    assert len(results) == 2

    good, evil = results[0], results[1]
    # control chars stripped from title/snippet
    assert good["title"] == "GoodSrc"
    assert good["snippet"] == "snip"
    assert good["url"] == "https://ex.com/a"
    # javascript: URL neutralized (dropped to empty)
    assert evil["url"] == ""

    # both persisted as web_clip assets
    assert good.get("artifact_id", "").startswith("art_")
    assert evil.get("artifact_id", "").startswith("art_")
    art = await store.get(good["artifact_id"])
    assert art.kind == "web_clip"
    assert art.trust == "unverified"
    assert art.inject == "stub"
    assert art.source == "web_search"
    assert art.created_by_turn == "t_web"
    # session-pinned (citeable / surfaces in Assets) — put_bytes wrote the row
    async with aiosqlite.connect(store.db_path) as db:
        cur = await db.execute(
            "SELECT 1 FROM session_attachments WHERE session_id='s_web' AND artifact_id=? AND detached_at IS NULL",
            (good["artifact_id"],),
        )
        assert await cur.fetchone() is not None
    # the javascript: payload is NOT in the stored clip body
    evil_art = await store.get(evil["artifact_id"])
    body = Path(evil_art.path).read_bytes().decode("utf-8")
    assert "javascript:" not in body


@pytest.mark.asyncio
async def test_web_clip_persist_fail_soft(
    store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store outage must NOT break the search return (fail-soft invariant)."""
    import httpx2

    monkeypatch.setattr(httpx2, "AsyncClient", _FakeClient)

    async def _boom(**kw):
        raise RuntimeError("store down")

    broken = SimpleNamespace(put_bytes=_boom)
    ctx = SimpleNamespace(
        artifact_store=broken,
        user_id="u1",
        session_id="s_web",
        turn_id="t_web",
        canvas_id=None,
        team_id="team_1",
    )
    out = await _team_handler()({"query": "q"}, ctx)
    # search still returns results, just without artifact_id
    assert len(out["results"]) == 2
    assert "artifact_id" not in out["results"][0]

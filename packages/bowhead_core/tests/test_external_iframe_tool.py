"""Tests for canvas.add_external_iframe (#125 Case A)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_saas_rejects_non_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    from bowhead_core.canvas_tools import _add_external_iframe

    result = await _add_external_iframe(
        {"url": "http://example.com", "title": "X"},
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert "allowed for this deployment" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,<b>x</b>", "file:///etc/passwd", "//evil.com/x"])
async def test_local_rejects_non_web_schemes(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    from bowhead_core.canvas_tools import _add_external_iframe

    result = await _add_external_iframe(
        {"url": url, "title": "X"},
        SimpleNamespace(canvas_store=None),  # type: ignore[arg-type]
    )
    assert result["error"] == "url must be an http(s) web page"


@pytest.mark.asyncio
async def test_rejects_empty_url() -> None:
    from bowhead_core.canvas_tools import _add_external_iframe

    result = await _add_external_iframe(
        {"title": "X"},
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert result["error"] == "url required"


@pytest.mark.asyncio
async def test_happy_path() -> None:
    from bowhead_core.canvas_tools import _add_external_iframe

    stored = SimpleNamespace(
        id="sec_ext",
        seq=1,
        to_report_section=lambda: {"id": "sec_ext", "kind": "external_iframe"},
    )

    async def _ensure(**kw: object) -> object:
        return SimpleNamespace(id="cv_1")

    async def _append(**kw: object) -> object:
        return stored

    ctx = SimpleNamespace(
        canvas_store=SimpleNamespace(
            ensure_canvas=_ensure,
            append_section=_append,
        ),
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        turn_id="t1",
        parent_bus=None,
    )
    result = await _add_external_iframe(
        {"url": "https://example.com/dash", "title": "Dashboard", "height": 600},
        ctx,  # type: ignore[arg-type]
    )
    assert result["kind"] == "external_iframe"
    assert result["url"] == "https://example.com/dash"
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_height_clamped() -> None:
    from bowhead_core.canvas_tools import _add_external_iframe

    stored_payload: dict = {}

    async def _ensure(**kw: object) -> object:
        return SimpleNamespace(id="cv_1")

    async def _append(**kw: object) -> object:
        stored_payload.update(kw.get("payload") or {})  # type: ignore[arg-type]
        return SimpleNamespace(id="sec_x", seq=1, to_report_section=dict)

    ctx = SimpleNamespace(
        canvas_store=SimpleNamespace(
            ensure_canvas=_ensure,
            append_section=_append,
        ),
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        turn_id="t1",
        parent_bus=None,
    )
    # Large height is clamped to 1200.
    await _add_external_iframe(
        {"url": "https://x.com", "title": "X", "height": 99999},
        ctx,  # type: ignore[arg-type]
    )
    assert stored_payload["height"] == 1200


@pytest.mark.asyncio
async def test_local_accepts_intranet_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    from bowhead_core.canvas_tools import _add_external_iframe

    async def _ensure(**kw: object) -> object:
        return SimpleNamespace(id="cv_1")

    async def _append(**kw: object) -> object:
        return SimpleNamespace(id="sec_x", seq=1, to_report_section=dict)

    ctx = SimpleNamespace(
        canvas_store=SimpleNamespace(ensure_canvas=_ensure, append_section=_append),
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        turn_id="t1",
        parent_bus=None,
    )
    result = await _add_external_iframe(
        {"url": "http://10.0.0.5:8080/dash", "title": "BI"},
        ctx,  # type: ignore[arg-type]
    )
    assert result.get("error") is None
    assert result["url"] == "http://10.0.0.5:8080/dash"

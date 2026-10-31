"""Tests for canvas.add_html_artifact (#83 Case B)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_add_html_artifact_rejects_missing_artifact_id() -> None:
    from bowhead_core.canvas_tools import _add_html_artifact

    result = await _add_html_artifact({}, SimpleNamespace())  # type: ignore[arg-type]
    assert result["error"] == "artifact_id required"


@pytest.mark.asyncio
async def test_add_html_artifact_rejects_wrong_mime() -> None:
    from bowhead_core.canvas_tools import _add_html_artifact

    fake_art = SimpleNamespace(
        mime="application/json",
        filename="data.json",
        size=100,
    )

    # #54 long-term fix: artifact_store.get adds check_created_by; mock accepts arbitrary kwargs.
    async def _get(aid: str, **_kw: object) -> object:
        return fake_art

    ctx = SimpleNamespace(
        artifact_store=SimpleNamespace(get=_get),
        canvas_store=SimpleNamespace(),
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        turn_id="t1",
    )
    result = await _add_html_artifact(
        {"artifact_id": "art_1", "title": "Test"},
        ctx,  # type: ignore[arg-type]
    )
    assert "not html" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [5 * 1024 * 1024, 1024 * 1024])
async def test_add_html_artifact_rejects_oversized(monkeypatch, limit: int) -> None:
    from bowhead_core.canvas_tools import _add_html_artifact

    monkeypatch.setenv("BOWHEAD_CANVAS_HTML_MAX_BYTES", str(limit))
    fake_art = SimpleNamespace(
        mime="text/html",
        filename="big.html",
        size=limit + 1,
    )

    # #54 long-term fix: artifact_store.get adds check_created_by; mock accepts arbitrary kwargs.
    async def _get(aid: str, **_kw: object) -> object:
        return fake_art

    ctx = SimpleNamespace(
        artifact_store=SimpleNamespace(get=_get),
        canvas_store=SimpleNamespace(),
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        turn_id="t1",
    )
    result = await _add_html_artifact(
        {"artifact_id": "art_1", "title": "Big"},
        ctx,  # type: ignore[arg-type]
    )
    assert "too large" in result["error"]
    assert result["maximum_bytes"] == limit


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [10 * 1024, 600 * 1024, 6 * 1024 * 1024, 25 * 1024 * 1024])
async def test_add_html_artifact_happy_path(size: int) -> None:
    from bowhead_core.canvas_tools import _add_html_artifact

    fake_art = SimpleNamespace(
        mime="text/html",
        filename="page.html",
        size=size,
    )
    stored = SimpleNamespace(
        id="sec_xxx",
        seq=1,
        to_report_section=lambda: {"id": "sec_xxx", "kind": "html_artifact"},
    )

    # #54 long-term fix: artifact_store.get adds check_created_by; mock accepts arbitrary kwargs.
    async def _get(aid: str, **_kw: object) -> object:
        return fake_art

    async def _ensure(**kw: object) -> object:
        return SimpleNamespace(id="cv_1")

    async def _append(**kw: object) -> object:
        return stored

    ctx = SimpleNamespace(
        artifact_store=SimpleNamespace(get=_get),
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
    result = await _add_html_artifact(
        {"artifact_id": "art_1", "title": "Dashboard Demo", "height": 600},
        ctx,  # type: ignore[arg-type]
    )
    assert result["kind"] == "html_artifact"
    assert result["section_id"] == "sec_xxx"
    assert result["artifact_id"] == "art_1"
    assert result["title"] == "Dashboard Demo"


@pytest.mark.asyncio
async def test_team_html_limit_overrides_deployment_default():
    from unittest.mock import AsyncMock

    from bowhead_core.canvas_tools import _add_html_artifact
    from bowhead_core.execution_limits import ExecutionLimits, execution_limits_scope

    art = SimpleNamespace(mime="text/html", filename="large.html", size=6 * 1024 * 1024)
    section = SimpleNamespace(id="sec_html", seq=1, to_report_section=lambda: {"id": "sec_html", "kind": "html_artifact"})
    canvas_store = SimpleNamespace(
        ensure_canvas=AsyncMock(return_value=SimpleNamespace(id="cv_report")), append_section=AsyncMock(return_value=section)
    )
    ctx = SimpleNamespace(
        artifact_store=SimpleNamespace(get=AsyncMock(return_value=art)),
        canvas_store=canvas_store,
        session_id="s_report",
        workspace_id="ws_report",
        user_id="u_owner",
        turn_id="t_report",
        parent_bus=None,
    )
    with execution_limits_scope(ExecutionLimits(120, 600, 8 * 1024 * 1024)):
        result = await _add_html_artifact({"artifact_id": "art_large"}, ctx)
    assert result["kind"] == "html_artifact"
    with execution_limits_scope(ExecutionLimits(120, 600, 4 * 1024 * 1024)):
        rejected = await _add_html_artifact({"artifact_id": "art_large"}, ctx)
    assert rejected["code"] == "html_artifact_too_large"
    assert rejected["maximum_bytes"] == 4 * 1024 * 1024
    assert canvas_store.append_section.await_count == 1


@pytest.mark.asyncio
async def test_add_html_artifact_rejects_filename_suffix_when_mime_wrong() -> None:
    """Active HTML never upgrades from an extension-only guess."""
    from bowhead_core.canvas_tools import _add_html_artifact

    fake_art = SimpleNamespace(
        mime="application/octet-stream",
        filename="generated.html",
        size=5000,
    )

    async def _get(aid: str, **_kw: object) -> object:
        return fake_art

    ctx = SimpleNamespace(
        artifact_store=SimpleNamespace(get=_get),
        canvas_store=SimpleNamespace(),
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        turn_id="t1",
        parent_bus=None,
    )
    result = await _add_html_artifact(
        {"artifact_id": "art_1", "title": "T"},
        ctx,  # type: ignore[arg-type]
    )
    assert "not html" in result["error"]


def _capturing_ctx(captured: dict[str, object]) -> object:
    """Context whose append_section records the persisted provenance payload."""
    fake_art = SimpleNamespace(mime="text/html", filename="page.html", size=4096)
    stored = SimpleNamespace(id="sec_s4", seq=2, to_report_section=lambda: {"id": "sec_s4"})

    async def _get(aid: str, **_kw: object) -> object:
        return fake_art

    async def _ensure(**kw: object) -> object:
        return SimpleNamespace(id="cv_1")

    async def _append(**kw: object) -> object:
        captured["payload"] = kw.get("payload")
        return stored

    return SimpleNamespace(
        artifact_store=SimpleNamespace(get=_get),
        canvas_store=SimpleNamespace(ensure_canvas=_ensure, append_section=_append),
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        turn_id="t1",
        parent_bus=None,
    )


@pytest.mark.asyncio
async def test_add_html_artifact_no_data_marks_unverified() -> None:
    """No generation-data snapshot remains explicitly unchecked."""
    from bowhead_core.canvas_tools import _add_html_artifact

    cap: dict[str, object] = {}
    await _add_html_artifact(
        {"artifact_id": "art_1", "title": "Sim"},
        _capturing_ctx(cap),  # type: ignore[arg-type]
    )
    payload = cap["payload"]
    assert payload["unverified"] is True  # type: ignore[index]
    assert "data" not in payload  # type: ignore[operator]


@pytest.mark.asyncio
async def test_add_html_artifact_with_data_is_verified() -> None:
    """Generation-data snapshot and citations remain persisted as provenance."""
    from bowhead_core.canvas_tools import _add_html_artifact

    cap: dict[str, object] = {}
    await _add_html_artifact(
        {
            "artifact_id": "art_1",
            "title": "Sim",
            "data": {"baseline_margin": 61.2, "cogs": 38.8},
            "sources": [2, 6, 6.0],
        },
        _capturing_ctx(cap),  # type: ignore[arg-type]
    )
    payload = cap["payload"]
    assert payload["unverified"] is False  # type: ignore[index]
    assert payload["data"] == {"baseline_margin": 61.2, "cogs": 38.8}  # type: ignore[index]
    assert payload["sources"] == [2, 6, 6]  # type: ignore[index]


@pytest.mark.asyncio
async def test_add_html_artifact_tolerates_scalar_sources() -> None:
    """LLM 违约把 sources 传成标量(6)而非 [6] —— 不能 TypeError 崩, 降级为空 sources."""
    from bowhead_core.canvas_tools import _add_html_artifact

    cap: dict[str, object] = {}
    result = await _add_html_artifact(
        {"artifact_id": "art_1", "title": "Sim", "data": {"x": 1}, "sources": 6},
        _capturing_ctx(cap),  # type: ignore[arg-type]
    )
    assert result.get("error") is None
    assert cap["payload"].get("sources") is None  # type: ignore[union-attr]
    # bool is not a source number.
    cap2: dict[str, object] = {}
    await _add_html_artifact(
        {"artifact_id": "art_1", "title": "S", "data": {"x": 1}, "sources": [2, True, 6]},
        _capturing_ctx(cap2),  # type: ignore[arg-type]
    )
    assert cap2["payload"]["sources"] == [2, 6]  # type: ignore[index]


def test_clean_artifact_data_bounds() -> None:
    """非 dict / 空 / 超 200KB → None(降级 unverified); 合法 → 原样返回."""
    from bowhead_core.canvas_tools import _HTML_ARTIFACT_DATA_MAX, _clean_artifact_data

    assert _clean_artifact_data(None) is None
    assert _clean_artifact_data([1, 2, 3]) is None  # Top level must be an object.
    assert _clean_artifact_data({}) is None  # Empty dict is meaningless.
    ok = {"a": 1, "b": "x"}
    assert _clean_artifact_data(ok) == ok
    huge = {"k": "x" * (_HTML_ARTIFACT_DATA_MAX + 10)}
    assert _clean_artifact_data(huge) is None

"""Smoke tests for bowhead_core.turn_runner module (#123 step 3).

仅验证 free helpers 从 turn_runner 可导且行为和原 orchestrator 搬迁前一致.
（业务逻辑细测已在 test_orchestrator.py / test_orchestrator_critical_paths.py 覆盖过.）
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_abbrev_thinking_short_text_pass_through() -> None:
    from bowhead_core.turn_runner import abbrev_thinking

    assert abbrev_thinking("短") == "短"
    assert abbrev_thinking("") == ""


def test_abbrev_thinking_long_text_abbreviated() -> None:
    from bowhead_core.turn_runner import abbrev_thinking

    long = "a" * 500
    out = abbrev_thinking(long, head=100, tail=50)
    assert "省略" in out
    assert len(out) < len(long)


def test_subagent_backend_env_default() -> None:
    import os

    from bowhead_core.turn_runner import subagent_backend_from_env

    # Keep env orderly; no setting defaults legacy.
    os.environ.pop("BOWHEAD_SUBAGENT_BACKEND", None)
    assert subagent_backend_from_env() == "legacy"


def test_flatten_for_summary_expands_nested() -> None:
    from bowhead_core.turn_runner import flatten_for_summary

    nested = [
        SimpleNamespace(tool_name="bi.ask", result={}, error=None, nested=None),
        SimpleNamespace(tool_name="bi.fetch", result={}, error=None, nested=None),
    ]
    top = [
        SimpleNamespace(tool_name="subagent_spawn", result={}, error=None, nested=nested),
    ]
    flat = flatten_for_summary(top)
    assert len(flat) == 3  # 1 top + 2 nested


def test_data_attachment_ids_detects_csv_and_xlsx() -> None:
    from bowhead_core.turn_runner import data_attachment_ids

    prepared = [
        SimpleNamespace(mime="text/csv", filename="a.csv", artifact_id="art_1"),
        SimpleNamespace(mime="", filename="b.xlsx", artifact_id="art_2"),
        SimpleNamespace(mime="image/png", filename="c.png", artifact_id="art_3"),
    ]
    ids = data_attachment_ids(prepared)
    assert set(ids) == {"art_1", "art_2"}


def test_user_explicitly_wants_bi_bypass_keywords() -> None:
    from bowhead_core.turn_runner import user_explicitly_wants_bi

    assert user_explicitly_wants_bi("基于附件名单，去 datarc 查他们今年消费")
    assert user_explicitly_wants_bi("from datarc pull last year")
    assert not user_explicitly_wants_bi("分析这份附件")


def test_safe_preview_truncates_long_json() -> None:
    from bowhead_core.turn_runner import safe_preview

    big = {"rows": list(range(200))}
    out = safe_preview(big, max_len=100)
    assert len(out) <= 103  # 100 + "..."


def test_result_meta_on_list_returns_count() -> None:
    from bowhead_core.turn_runner import result_meta

    assert result_meta([1, 2, 3]) == {"type": "array", "count": 3}


def test_result_meta_on_metrics_dict() -> None:
    from bowhead_core.turn_runner import result_meta

    meta = result_meta({"metrics": [{"a": 1}, {"a": 2}], "type": "metric"})
    assert meta == {"type": "ask_question", "metric_count": 2, "q_type": "metric"}


def test_helpers_public_home_is_turn_runner() -> None:
    """#123 契约终态（2026-07-14）：helpers 转公开命名、正式家=turn_runner；
    orchestrator 的 re-export 兼容期结束（全仓无旧私名调用点，grep 证）。
    本测试钉新契约：公开名可导入且可调用。"""
    from bowhead_core.turn_runner import (
        data_attachment_ids,
        safe_preview,
        user_explicitly_wants_bi,
    )

    assert callable(data_attachment_ids)
    assert callable(user_explicitly_wants_bi)
    assert callable(safe_preview)


@pytest.mark.asyncio
async def test_turn_runner_stores_orch_reference() -> None:
    """#124/#128: TurnRunner 构造时只存 orch 引用，不做 io."""
    from types import SimpleNamespace as _SN

    from bowhead_core.turn_runner import TurnRunner

    orch = _SN(event_registry="stub")
    runner = TurnRunner(orch)  # type: ignore[arg-type]
    assert runner._orch is orch


@pytest.mark.asyncio
async def test_orchestrator_run_turn_routes_through_turnrunner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orchestrator.run_turn 应走 TurnRunner(self).run(req) —— #128 Step 3c 确认
    `_run_turn_internal` 已删，body 必须从 TurnRunner 进入."""
    from bowhead_core.orchestrator import Orchestrator

    from bowhead_core import turn_runner as tr_module

    orch = Orchestrator.__new__(Orchestrator)

    # Intercept TurnRunner.run to confirm it's the only path Orchestrator.run_turn uses
    calls = {"n": 0, "orch_ref": None, "req": None}

    async def _spy(self, req):
        calls["n"] += 1
        calls["orch_ref"] = self._orch
        calls["req"] = req
        return "ok_through_turnrunner"

    monkeypatch.setattr(tr_module.TurnRunner, "run", _spy)

    from types import SimpleNamespace as _SN

    req = _SN(turn_id="t_x")
    result = await orch.run_turn(req)  # type: ignore[arg-type]
    assert result == "ok_through_turnrunner"
    assert calls["n"] == 1
    assert calls["orch_ref"] is orch
    assert calls["req"] is req

    # Confirm _run_turn_internal absent (removed #128 Step 3c).
    assert not hasattr(orch, "_run_turn_internal"), "_run_turn_internal should be removed as of #128 Step 3c"

"""feedback_escalate 目标名解析：精确相等、可见性/审批门、同名多个报歧义而不是默默取第一个。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.feedback_escalate_tool import AmbiguousTarget
from bowhead_gateway.feedback_escalate_resolver import resolve_feedback_target


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def list_team_subagents(self, team_id, *, only_enabled=False):  # noqa: ANN001
        self.calls.append(("subagents", team_id, only_enabled))
        return [
            SimpleNamespace(id="osa_1", name="sample-analyst", display_name="Sample 数据分析师"),
            SimpleNamespace(id="osa_2", name="sample-analyst-v2", display_name="Sample 数据分析师"),
            SimpleNamespace(id="osa_3", name="eddy", display_name="Eddy"),
        ]

    async def list_team_skills(self, team_id, *, only_enabled=False, only_approved=True):  # noqa: ANN001
        self.calls.append(("skills", team_id, only_enabled, only_approved))
        return [
            SimpleNamespace(id="sk_1", name="chart-style", display_name="图表规范", visibility="team_wide"),
            SimpleNamespace(id="sk_2", name="mine", display_name="我的私有", visibility="creator_only"),
        ]


def _r(kind: str, name: str):
    return asyncio.run(resolve_feedback_target(_Store(), "t1", kind, name))


def test_exact_casefold_match_on_name_or_display_name() -> None:
    assert _r("agent_def", "eddy") == "osa_3" and _r("agent_def", "EDDY") == "osa_3"
    assert _r("agent_def", "Ed") is None  # No prefix/fuzzy matching.
    assert _r("team_skill", "图表规范") == "sk_1" and _r("team_skill", "CHART-STYLE") == "sk_1"


def test_creator_only_skill_and_unknown_kind_are_invisible() -> None:
    assert _r("team_skill", "我的私有") is None and _r("team_skill", "mine") is None
    assert _r("semantic", "净利润") is None and _r("agent_def", "   ") is None


def test_ambiguous_display_name_raises_with_labels() -> None:
    with pytest.raises(AmbiguousTarget) as exc:
        _r("agent_def", "Sample 数据分析师")
    assert set(exc.value.names) == {"Sample 数据分析师（sample-analyst）", "Sample 数据分析师（sample-analyst-v2）"}  # Same name: slug differs.


def test_queries_only_enabled_and_approved() -> None:
    store = _Store()
    asyncio.run(resolve_feedback_target(store, "t1", "team_skill", "图表规范"))
    asyncio.run(resolve_feedback_target(store, "t1", "agent_def", "eddy"))
    assert ("skills", "t1", True, True) in store.calls and ("subagents", "t1", True) in store.calls


def test_core_can_import_the_resolver_without_gateway() -> None:
    """自动进货口归 memory 线（core 侧）后，它必须能直接用这个解析器——core 不能 import gateway。

    这条钉的是**位置**不是行为：实现移到 `bowhead_core.feedback_target_resolver`，gateway 保留薄转发。
    移回 gateway 会让 core 侧的进货路径拿不到解析器，而那正是 `propose` 要 id 不要展示名的唯一出路。
    """

    import subprocess
    import sys

    # Import only core in a subprocess to verify no dependency on the gateway package.
    code = (
        "import sys;"
        "from bowhead_core.feedback_target_resolver import resolve_feedback_target, AmbiguousTarget;"
        "assert callable(resolve_feedback_target) and issubclass(AmbiguousTarget, Exception);"
        "assert not [m for m in sys.modules if m.startswith('bowhead_gateway')], sorted(m for m in sys.modules if m.startswith('bowhead_gateway'))"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    # Old gateway import path remains available (thin forwarding).
    from bowhead_gateway.feedback_escalate_resolver import resolve_feedback_target as forwarded

    assert forwarded is resolve_feedback_target

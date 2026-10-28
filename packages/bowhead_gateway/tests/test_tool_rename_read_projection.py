"""批 0 · C7 收口 —— subagent 读投影按**现名**返回 `tool_refs`。

单独放在 gateway 测试里：`bowhead_gateway` 不在 bowhead_core 的测试依赖里。
规范见 `docs/design/工具改名迁移.md`。
"""

from __future__ import annotations

import pytest
from bowhead_core.agents._generated.tool_renames import TOOL_RENAMES

pytestmark = pytest.mark.unit

_OLD = "zzz_legacy_probe_tool"
_NEW = "bi_query"


@pytest.fixture
def injected_rename():
    """空表下断言恒真 ⇒ 不注入的话这条用例是死的。"""
    TOOL_RENAMES[_OLD] = _NEW
    try:
        yield
    finally:
        TOOL_RENAMES.pop(_OLD, None)


def test_public_projection_returns_canonical_tool_refs(injected_rename) -> None:
    """判据 1c 的**真机**形态：前端拿的是 GET 的 `tool_refs`。

    前端把「不在 live catalog 里的 ref」判成 staleSel 并阻断保存
    （`create-subagent-card.tsx` 的 `canSubmit` 要求 `staleSel.length === 0`）
    ⇒ 只在 validator 里归一是不够的：读投影不归一，客户打开编辑界面
    就被逼先删掉那一行才能保存任何改动。库里一个字节都不动。
    """
    from bowhead_core.user_store import TeamSubagent
    from bowhead_gateway.routes.team_subagents import _public

    sa = TeamSubagent(
        id="sa_1",
        team_id="team_1",
        name="probe",
        tool_refs=[_OLD],
    )

    got = _public(sa)
    assert got["tool_refs"] == [_NEW], "读投影必须给现名，否则前端判它 stale"
    assert got["tool_count"] == 1

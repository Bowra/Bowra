"""反馈候选的**目标名 → id** 解析：调用方只拿到用户叫得出的名字，id 与可见性在这里判。

2026-09-13 从 gateway 移到 core：自动进货口归 memory 线之后，两条进货路径都要用它
（core 不能 import gateway），`propose` 要的 `target_ref` 是 id 不是展示名。gateway 侧保留
同名薄转发，老调用点不动。

- agent_def：团队 subagent 的 display_name / name（casefold 精确相等，不做模糊）；
- team_skill：只认 team_wide 且已审批、启用的技能（creator_only 采纳是 admin 门，转过去谁都采不了）；
- 同名多个 ⇒ AmbiguousTarget（让 Lead 问一句，不能默默取第一个：候选会落到错的资源 owner 车道）。
"""

from __future__ import annotations

from typing import Any


class AmbiguousTarget(Exception):
    """同名多个目标：调用方据此问用户一句，不能默默取第一个（候选会落到错的资源 owner 车道）。

    ⚠ 历史位置在 `feedback_escalate_tool`，2026-09-13 随解析器一起移到这里；那边保留再导出。
    """

    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names


def _names(obj: Any) -> set[str]:
    return {str(v).casefold() for v in (getattr(obj, "display_name", None), getattr(obj, "name", None)) if v}


def _label(obj: Any) -> str:
    """展示名相同的两条要靠 name（slug）区分，否则歧义提示等于没说。"""

    display = str(getattr(obj, "display_name", None) or "")
    name = str(getattr(obj, "name", None) or "")
    if display and name and display.casefold() != name.casefold():
        return f"{display}（{name}）"
    return display or name or str(getattr(obj, "id", ""))


async def resolve_feedback_target(user_store: Any, team_id: str, kind: str, name: str) -> str | None:
    needle = name.strip().casefold()
    if not needle:
        return None
    if kind == "agent_def":
        rows = [ag for ag in await user_store.list_team_subagents(team_id, only_enabled=True) if needle in _names(ag)]
    elif kind == "team_skill":
        rows = [
            sk
            for sk in await user_store.list_team_skills(team_id, only_enabled=True)  # only_approved defaults to True.
            if (getattr(sk, "visibility", None) or "team_wide") == "team_wide" and needle in _names(sk)
        ]
    else:
        return None
    if not rows:
        return None
    if len(rows) > 1:
        raise AmbiguousTarget(sorted({_label(r) for r in rows}))
    return str(rows[0].id)

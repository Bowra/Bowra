"""Wiring guard — web_search 团队化恢复后 main.py 必须真的注册它，且在 role-binding pass 之前.

full-lifespan boot 依赖本机 .env 的持久 DB（schema 可能陈旧），不适合当 CI 硬门；这里
用源码解析守 main.py 的两条契约（同 test_datarc_per_turn_client_ctx 的 source-guard 风格）：

1. 注册块调用了 ``register_web_search_tool(..., team_search_resolver=...)``——团队解析器
   接线还在（不是无参空注册、也没被整段删掉）。
2. 该调用出现在 ``apply_paradigm_v2_role_bindings`` **之前**——否则 role-binding pass 跑完
   才注册，yaml→codegen 的 allowed_for_roles（lead/analyst）不会作用到 web_search。

运行期的角色门 + handler 行为由 test_web_search_lead_role / test_web_search 覆盖；ship 前
的真 boot 冒烟见 sprint 计划验收项。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_MAIN = Path(__file__).resolve().parents[1] / "src" / "bowhead_gateway" / "main.py"


@pytest.mark.unit
def test_main_registers_web_search_before_role_bindings() -> None:
    src = _MAIN.read_text(encoding="utf-8")

    reg_at = src.find("register_web_search_tool(tool_registry")
    assert reg_at != -1, "main.py 未调用 register_web_search_tool(tool_registry, ...)——团队化恢复的接线丢了"
    assert "team_search_resolver=" in src[reg_at : reg_at + 200], "register_web_search_tool 未传 team_search_resolver——会退回不注册"

    bind_at = src.find("apply_paradigm_v2_role_bindings(tool_registry)")
    assert bind_at != -1, "main.py 未调用 apply_paradigm_v2_role_bindings(tool_registry)"
    assert reg_at < bind_at, "register_web_search_tool 必须在 apply_paradigm_v2_role_bindings 之前，否则 lead/analyst 角色绑定不生效"

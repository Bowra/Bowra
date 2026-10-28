"""增长 / 同比口径 helper 必须在 **boot 后的 live compute prompt** 里、且 import 写法对得上真模块。

manifest 在 boot 时同名覆盖 `PROFILES`，只扫源码会在改错文件时照样绿；所以闸打在 boot 之后。
import 行写错（模块名 / 函数名）= 模型照抄即 ImportError，所以同时核对 runner 真的会 stage 这个模块。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "packages/bowhead_skill/catalog/python_sandbox/scripts"


@pytest.mark.unit
def test_live_compute_prompt_routes_growth_questions_to_the_staged_helper() -> None:
    from bowhead_core.agents.agent_runner_adapter import register_all_system_manifests
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.subagent import PROFILES

    register_all_system_manifests(AgentRegistry.bootstrap())
    prompt = PROFILES["compute"].system_prompt
    m = re.search(r"from (\w+) import (\w+)\n\s*r = \2\(", prompt)
    assert m, "live compute prompt 里没有增长 helper 的用法"
    module, func = m.groups()
    runner = (_SCRIPTS / "runner.py").read_text(encoding="utf-8")
    assert f'"{module}.py"' in runner, f"runner 不会把 {module}.py stage 进沙箱"
    assert f"def {func}(" in (_SCRIPTS / f"{module}.py").read_text(encoding="utf-8")


@pytest.mark.unit
def test_prior_year_fetch_rule_sits_in_the_fetch_stage() -> None:
    """「比增长要连上一年一起取」必须写在 ③ 取数，不能只在 ④ 算。

    116 真机（`t_8e10f49f6e9a`）：compute 走简单题路径、不读 SOP，读完 ③ 就只取了本年；
    规则只写在 ④ 时，读到它已经晚了一个阶段。
    """
    from bowhead_core.agents.agent_runner_adapter import register_all_system_manifests
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.subagent import PROFILES

    register_all_system_manifests(AgentRegistry.bootstrap())
    prompt = PROFILES["compute"].system_prompt
    fetch, compute = prompt.index("## ③"), prompt.index("## ④")
    rule = prompt.find("第一批 `bi_query` 就把上一年同一指标")
    assert fetch < rule < compute

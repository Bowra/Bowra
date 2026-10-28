"""能力协议块的选中规则与装配位置。

★ 本文件的四类用例都是**为了让某个 bug 能红**而写的，不是覆盖率：

1. **正对照**：没有取数工具的执行体拿不到取数协议。缺了它，「所有人都拿到所有协议」
   这个 bug 不会红——而 `capi_report_canvas.required_tools` 恰恰是空的，`⊆` 对空集恒真。
2. **突变**：把 Implementation 的 `required_tools` 换成一个不存在的工具名，协议必须消失。
   缺了它，「解析恒真」不会红。
3. **逐字节稳定**：同样的工具面连渲两次必须字节相同，且块落在**稳定前缀**而非本轮尾巴。
   缺了它，缓存被打碎不会红。
4. **单一装配点**：`subagent.py` 里只允许有一处装配。缺了它，将来有人再造一个装配面
   （本仓「一个概念 N 处实现」的老毛病）不会红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from bowhead_core.agent_artifact_capability import reset_artifact_registry_cache_for_test
from bowhead_core.agent_capability_catalog import CapabilityCatalog
from bowhead_core.agents.capability_protocols import render_capability_protocols
from bowhead_core.context_assembly import split_system_for_prefix

_DATA_TOOLS = ("bi_query", "bi_schema", "bi_concept_resolve")
_DATA_AGENT = (*_DATA_TOOLS, "add_chart", "write_report", "read_report", "subagent_return")


def _headings(block: str) -> set[str]:
    return {line.strip() for line in block.splitlines() if line.startswith("# ")}


def test_data_protocol_requires_the_data_tool() -> None:
    """正对照：手里没有 `bi_query` 的执行体一个字的取数协议都不该拿到。"""
    with_data = _headings(render_capability_protocols(_DATA_AGENT))
    without_data = _headings(render_capability_protocols(["add_chart", "write_report", "subagent_return"]))

    assert "# Data query protocol" in with_data
    assert "# Data query protocol" not in without_data
    # Both canvas blocks remain, proving the difference is classification, not a missing entire render.
    assert "# Delivery protocol" in with_data
    assert "# Delivery protocol" in without_data


def test_no_canvas_tools_means_no_protocol_at_all() -> None:
    """最硬的正对照：既不取数也不上画布 ⇒ 空串，调用方整段省略。"""
    assert render_capability_protocols(["kb_search", "subagent_return"]) == ""


def test_partial_datarc_grant_keeps_validation_without_inventing_prerequisite_tools() -> None:
    block = render_capability_protocols(["bi_query", "subagent_return"])
    assert "applies only to Datarc queries through `bi_query`" in block
    assert "Use a named Tool below only when it is available" in block
    assert "report the missing evidence instead of guessing fields or inventing a call" in block
    assert "Inspect the parse report on each result" in block
    assert "Call `bi_schema` once" in block


def test_report_authoring_is_anchored_to_the_writing_tool() -> None:
    """`consultant` 形态（有 `add_chart`、没有 `write_report`）不该被教怎么写正文。

    这正是本 sprint 要消灭的形态：教一个它调不到的工具（discovery §2.3 的 datarc hint）。
    """
    without_pen = _headings(render_capability_protocols(["bi_query", "add_chart", "subagent_return"]))
    with_pen = _headings(render_capability_protocols(["bi_query", "add_chart", "write_report", "subagent_return"]))

    assert "# Report authoring protocol" not in without_pen
    assert "# Report authoring protocol" in with_pen
    assert "# Delivery protocol" in without_pen  # Still supply the delivery contract.


def test_legacy_tool_name_still_resolves() -> None:
    """旧工具名必须先归一。不归一 ⇒ 判成「不具备该能力」，而 dispatch 层测试全绿。"""
    assert _headings(render_capability_protocols(["canvas_add_chart"])) == _headings(render_capability_protocols(["add_chart"]))


def test_mutating_required_tools_removes_the_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """突变：把取数实现的 `required_tools` 换成不存在的工具，协议必须消失。

    没有这条，「解析恒真」（例如误把空集当命中）会一路全绿。
    """
    import bowhead_core.agent_capability_catalog as catalog_module

    source = Path(catalog_module.__file__).parent / "agents" / "manifests" / "capabilities" / "catalog-v1.yaml"
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    for implementation in payload["implementations"]:
        if implementation["id"] == "capi_data_query_native":
            implementation["required_tools"] = ["tool_that_does_not_exist"]
    mutated_dir = tmp_path / "capabilities"
    mutated_dir.mkdir()
    (mutated_dir / "catalog-v1.yaml").write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    mutated = CapabilityCatalog.bootstrap(manifest_dir=mutated_dir)
    monkeypatch.setattr(
        "bowhead_core.agent_capability_catalog.default_catalog",
        lambda: mutated,
    )

    assert "# Data query protocol" not in _headings(render_capability_protocols(_DATA_AGENT))


def test_empty_required_tools_is_not_a_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空 `required_tools` 的 Implementation 不算命中——空集的 `⊆` 恒真。

    ★ 这条是补写的：第一轮突变演练里，「把 `bool(required) and` 去掉」**没有让任何用例变红**，
    因为画布那三份模块都带 `anchor_tools`，锚分支把 impl 分支遮住了。于是这道闸实际上
    零覆盖——而 `capi_report_canvas.required_tools` 恰恰就是空的，正是它要防的形状。
    这里把取数实现改成空依赖（它的模块无锚），断言一个**一件工具都没有**的执行体拿不到它。
    """
    import bowhead_core.agent_capability_catalog as catalog_module

    source = Path(catalog_module.__file__).parent / "agents" / "manifests" / "capabilities" / "catalog-v1.yaml"
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    for implementation in payload["implementations"]:
        if implementation["id"] == "capi_data_query_native":
            implementation["required_tools"] = []
    mutated_dir = tmp_path / "capabilities"
    mutated_dir.mkdir()
    (mutated_dir / "catalog-v1.yaml").write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    mutated = CapabilityCatalog.bootstrap(manifest_dir=mutated_dir)
    monkeypatch.setattr("bowhead_core.agent_capability_catalog.default_catalog", lambda: mutated)

    assert render_capability_protocols([]) == ""
    assert "# Data query protocol" not in _headings(render_capability_protocols(["subagent_return"]))


def test_block_is_byte_stable_and_lands_in_the_cacheable_prefix() -> None:
    """同一工具面两次渲染字节相同，且落在稳定前缀——否则会打碎 prompt 缓存。"""
    first = render_capability_protocols(_DATA_AGENT)
    second = render_capability_protocols(list(reversed(_DATA_AGENT)))
    assert first == second
    assert first

    split = split_system_for_prefix(
        profile_prompt="IDENTITY",
        extra_instructions_block="",
        memory_block="MEMORY",
        language_block="",
        capability_block=first,
    )
    assert first in split.stable_system
    assert first not in split.turn_tail
    # Identity precedes rules: protocol blocks follow the profile prompt and precede the memory block.
    assert split.stable_system.index("IDENTITY") < split.stable_system.index(first) < split.stable_system.index("MEMORY")


def test_empty_block_keeps_the_prompt_byte_identical() -> None:
    """Definition field off ⇒ empty block ⇒ assembly is byte-identical to the pre-change prompt."""
    blocks = {
        "profile_prompt": "IDENTITY",
        "extra_instructions_block": "",
        "memory_block": "",
        "language_block": "",
    }
    assert split_system_for_prefix(**blocks) == split_system_for_prefix(**blocks, capability_block="")


def test_single_assembly_site() -> None:
    """装配只能有一处。

    两条派发路（Lead 直接 spawn / 计划步经 `agent_runner_adapter`）都汇合到
    `SubagentRunner.run`，所以**一处接线两侧都覆盖**；反过来，出现第二处就说明
    有人又造了一个平行装配面。
    """
    source = Path(__file__).resolve().parents[1] / "src" / "bowhead_core" / "subagent.py"
    text = source.read_text(encoding="utf-8")
    assert len(re.findall(r"^\s*_blk_capability = render_capability_protocols\(", text, re.MULTILINE)) == 1
    assert len(re.findall(r"capability_block=", text)) == 1


@pytest.fixture(autouse=True)
def _reset_registry_cache() -> None:
    reset_artifact_registry_cache_for_test()

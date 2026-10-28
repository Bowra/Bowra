"""T1#7 (connector-context 修正) — namespace→connector 映射表全覆盖 pin.

契约（SSOT docs/tool-context-framework.md v2）：
- `CONNECTOR_NAMESPACES` 是 tool 名前缀 → connector 类型 id 的唯一映射源；
  `_tool_requires_datarc/_tool_requires_pbi` 只是它的谓词投影（保名兼容测试/文档）。
- 真 registry 枚举全部注册 tool 名，逐个断言映射与前缀一致——防「新 tool 挂了
  connector 前缀却没进表」或「表与谓词漂移」。
- pbi_* 与其余内置工具一并由 build_default_registry 注册；测试枚举真实 registry，防止
  pbi 覆盖面静默为零（no-silent-caps）。
"""

from __future__ import annotations

from bowhead_core.tool_registry import (
    CONNECTOR_DISPLAY_NAMES,
    CONNECTOR_NAMESPACES,
    _tool_requires_datarc,
    build_default_registry,
    connector_for_tool,
)


def _all_registered_names() -> list[str]:
    reg = build_default_registry()
    return sorted(reg._specs)


def test_mapping_covers_every_registered_tool() -> None:
    names = _all_registered_names()
    assert names, "registry 枚举为空——build_default_registry 坏了"
    for name in names:
        expected = next(
            (conn for prefix, conn in CONNECTOR_NAMESPACES.items() if name.startswith(prefix)),
            None,
        )
        assert connector_for_tool(name) == expected, f"tool {name!r}: connector_for_tool 与 CONNECTOR_NAMESPACES 前缀语义漂移"


def test_coverage_not_silently_zero() -> None:
    """no-silent-caps：两个 namespace 都必须有真样本。"""
    names = _all_registered_names()
    assert any(n.startswith("bi_") for n in names), "bi_* 枚举为零——datarc 覆盖面假绿"


def test_requires_predicates_parity_with_table() -> None:
    """_tool_requires_* 与映射表 parity（防谓词被改回独立 if 链后漂移）."""
    probes = [*_all_registered_names(), "kb_search", "subagent_spawn", "teammcp.linear.create_issue"]
    for name in probes:
        assert _tool_requires_datarc(name) == (connector_for_tool(name) == "datarc")


def test_display_names_closed_over_namespace_values() -> None:
    """DISPLAY_NAMES 键闭包于 NAMESPACES 值域（每个 connector 必有用户面显示名）."""
    assert set(CONNECTOR_DISPLAY_NAMES) == set(CONNECTOR_NAMESPACES.values())


def test_overlay_tools_stay_outside_table() -> None:
    """多实例 connector（teammcp.<src>.*）判别符=实例 id，不走本表——契约锁 None."""
    assert connector_for_tool("teammcp.linear.create_issue") is None
    assert connector_for_tool("mcp.anything") is None


def test_binding_attrs_are_real_ctx_members() -> None:
    """T2#9 表有效性 pin：binding 的 *_attr 必须是 ToolExecutionContext 真实
    字段/方法（防 typo 静默 getattr None）；connector/display_name 与两张表一致."""
    import dataclasses

    from bowhead_core.tool_registry import (
        CONNECTOR_CONTEXT_BINDINGS,
        ToolExecutionContext,
    )

    field_names = {f.name for f in dataclasses.fields(ToolExecutionContext)}
    assert CONNECTOR_CONTEXT_BINDINGS, "绑定表为空——sentinel/switch 全灭"
    for key, b in CONNECTOR_CONTEXT_BINDINGS.items():
        assert key == b.connector, f"表 key {key!r} 与 binding.connector {b.connector!r} 不一致"
        for attr in (b.accessible_attr, b.switcher_attr, b.active_id_attr, b.active_name_attr):
            assert attr in field_names, f"{b.connector}: {attr!r} 不是 ToolExecutionContext 字段"
        assert callable(getattr(ToolExecutionContext, b.resolve_attr, None)), f"{b.connector}: {b.resolve_attr!r} 不是 ctx 方法"
        assert b.connector in set(CONNECTOR_NAMESPACES.values()), f"{b.connector!r} ∉ CONNECTOR_NAMESPACES 值域"
        assert CONNECTOR_DISPLAY_NAMES.get(b.connector) == b.display_name

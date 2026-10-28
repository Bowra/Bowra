"""bi-absent-turn Phase 2 (R1+R3) — BI 三值判据 + 缺席原因分流.

守卫面（docs/sprints/bi-absent-turn-2026-07-29/prd.md R6.1-R6.4）：
- R6.1 bi_availability() 真值表（全函数，含 N==1 desync 格）
- R6.2 prompt pin：缺陷 A 的复发面——「client 绑定 + 用户零可及」必须注入
  <no_data_source>（旧 has_datarc() 判据对此恒 False→不注入，单测曾不可见）
- R6.3 dispatch 闸按 datarc_absence_reason 分流；transient 严禁说成无权限
- R6.4 grep-gate：turn_runner 两处镜像 ctx 构造点都接了新字段
"""

from __future__ import annotations

import pathlib

import pytest
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.tool_registry import (
    BI_ABSENT,
    BI_AVAILABLE,
    BI_NEEDS_SELECTION,
    ToolExecutionContext,
    ToolRegistry,
    build_default_registry,
)

WS2 = ({"id": "42", "name": "零售集团"}, {"id": "7", "name": "华东大区"})


def _ctx(
    *,
    datarc: object | None,
    active: str | None,
    accessible: tuple = (),
    absence_reason: str = "",
) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_x",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=datarc,
        active_datarc_project=active,
        accessible_datarc_workspaces=accessible,
        datarc_absence_reason=absence_reason,
    )


# ─── R6.1 truth table ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("datarc", "active", "accessible", "expected"),
    [
        # Bound client + resolved project → available (accessible count is irrelevant)
        (object(), "42", (), BI_AVAILABLE),
        (object(), "42", WS2, BI_AVAILABLE),
        # Bound client + no project + >1 accessible → needs_selection (card path)
        (object(), None, WS2, BI_NEEDS_SELECTION),
        # Bound client + no project + 0/1 accessible → absent (defect A's main case + N==1 desync case)
        (object(), None, (), BI_ABSENT),
        (object(), None, ({"id": "42", "name": "零售集团"},), BI_ABSENT),
        # Unbound client → absent (regardless of other fields)
        (None, None, (), BI_ABSENT),
        (None, "42", WS2, BI_ABSENT),
    ],
)
def test_bi_availability_truth_table(datarc, active, accessible, expected) -> None:
    assert _ctx(datarc=datarc, active=active, accessible=accessible).bi_availability() == expected


# ─── R6.2 prompt pin (defect A recurrence surface) ────────────────────────


def test_prompt_notice_injected_when_client_bound_but_zero_accessible() -> None:
    """缺陷 A 的确切形状：team 有连接（旧判据 has_datarc()=True→不注入），用户零可及。

    修后 prompt 判据 = bi_availability()==absent → 必须注入 <no_data_source>。
    这是 test_prompts_lead.py:241/249 两个 pin 没钉住的那一格（它们只钉布尔接线）。
    """
    ctx = _ctx(datarc=object(), active=None, accessible=())
    assert ctx.bi_availability() == BI_ABSENT
    prompt = build_lead_system_prompt("ws_x", facts=[], has_datarc=False)
    assert "<no_data_source>" in prompt


def test_prompt_notice_variant_zero_workspaces() -> None:
    prompt = build_lead_system_prompt("ws_x", facts=[], has_datarc=False, no_datarc_reason="zero_workspaces")
    assert "<no_data_source>" in prompt
    assert "add them to the relevant Datarc workspace" in prompt
    assert "user-permission issue" in prompt
    # Do not direct users to configure a data source (that is no_connection wording).
    assert "Settings → Data connections" not in prompt


def test_prompt_notice_variant_no_identity() -> None:
    prompt = build_lead_system_prompt("ws_x", facts=[], has_datarc=False, no_datarc_reason="no_identity")
    assert "enable their Datarc account first" in prompt
    assert "Settings → Data connections" not in prompt


def test_prompt_notice_variant_transient_never_blames_permission() -> None:
    prompt = build_lead_system_prompt("ws_x", facts=[], has_datarc=False, no_datarc_reason="transient")
    assert "<no_data_source>" in prompt
    assert "try again shortly" in prompt
    # Transient failures must not direct users to others or imply permission issues (PRD R3.3: prevent erroneous tickets).
    # The notice itself says not to direct users to an admin; assert absence only of **directive wording**.
    assert "ask them to have an admin" not in prompt
    assert "user-permission issue" not in prompt
    assert "direct the user to an admin" in prompt  # Negative wording remains (Markdown stars separate "do **not**").


def test_prompt_notice_default_reason_uses_the_connection_remedy() -> None:
    """Missing and unknown reasons use the same Datarc connection guidance."""
    base = build_lead_system_prompt("ws_x", facts=[], has_datarc=False)
    assert build_lead_system_prompt("ws_x", facts=[], has_datarc=False, no_datarc_reason="no_connection") == base
    assert build_lead_system_prompt("ws_x", facts=[], has_datarc=False, no_datarc_reason="unknown_future") == base
    assert "no usable Datarc connection for this turn" in base
    assert "applies only to Datarc" in base


def test_prompt_notice_absent_when_bi_available() -> None:
    assert "<no_data_source>" not in build_lead_system_prompt("ws_x", facts=[], has_datarc=True)


# ─── R6.3 dispatch-gate wording branches ────────────────────────────────


@pytest.fixture(scope="module")
def reg() -> ToolRegistry:
    return build_default_registry()


async def _gate_error(reg: ToolRegistry, reason: str) -> str:
    ctx = _ctx(datarc=object(), active=None, accessible=(), absence_reason=reason)
    inv = await reg.execute("bi_schema", {}, ctx)
    assert inv.error is not None and inv.error.startswith("DatarcUnavailableError:")
    return inv.error


async def test_gate_no_identity_blames_user_permission_not_workspace(reg: ToolRegistry) -> None:
    err = await _gate_error(reg, "no_identity")
    assert "no" in err and "account" in err
    assert "USER-permission" in err
    assert "isn't linked" not in err  # No longer blames space configuration.


async def test_gate_zero_workspaces_asks_admin_grant(reg: ToolRegistry) -> None:
    err = await _gate_error(reg, "zero_workspaces")
    assert "NO workspace grants" in err
    assert "USER-permission" in err


async def test_gate_transient_never_claims_missing_permission(reg: ToolRegistry) -> None:
    err = await _gate_error(reg, "transient")
    assert "transient" in err
    assert "Do NOT claim the user lacks permission" in err
    assert "USER-permission issue" not in err


async def test_gate_default_reason_original_bytes(reg: ToolRegistry) -> None:
    """reason=""（legacy/headless）→ 原裸错逐字节不变（golden pin 同源）。"""
    err = await _gate_error(reg, "")
    assert err == (
        "DatarcUnavailableError: tool='bi_schema' needs a datarc project, but this workspace isn't linked to "
        "one. hint: answer from the knowledge base (kb_search / kb_query_records), "
        "or select a datarc project for this workspace."
    )


async def test_gate_sentinel_path_untouched_by_reason(reg: ToolRegistry) -> None:
    """可及 >1 时 sentinel 弹卡不受 reason 影响（R2.2 前置：弹卡路径恒在）。"""
    ctx = _ctx(datarc=object(), active=None, accessible=WS2, absence_reason="")
    inv = await reg.execute("bi_schema", {}, ctx)
    assert inv.error is None
    assert inv.result["error_kind"] == "needs_clarification"


# ─── R3.5 localized step summaries (deterministic defense) ────────────────────────


def test_step_preview_localizes_datarc_unavailable() -> None:
    """dispatch 闸拒绝 = 预期内能力缺席——用户可见摘要中文化；LLM 面英文原文不动。
    这是与「LLM 是否听 prompt 劝」无关的防线（客户 trace seq 1 的裸英文形状）。"""
    from bowhead_core.subagent import _prettify_output_preview

    out = _prettify_output_preview("bi_schema", None, "DatarcUnavailableError: tool='bi_schema' needs a datarc project…", ui_language="zh-CN")
    assert out == "BI 数据源在本轮不可用（说明见回复）"
    assert "DatarcUnavailableError" not in out
    out_pbi = _prettify_output_preview("pbi_query", None, "PbiUnavailableError: tool='pbi_query' needs a Power BI connection…", ui_language="zh-CN")
    assert out_pbi == "BI 数据源在本轮不可用（说明见回复）"
    # Other errors retain the original "失败: " prefix shape.
    assert _prettify_output_preview("bi_query", None, "BIValidationError: bad metric", ui_language="zh-CN").startswith("失败: ")
    # No interface language on the turn → English fallback.
    assert _prettify_output_preview("bi_schema", None, "DatarcUnavailableError: tool='bi_schema' needs a datarc project…") == (
        "BI source unavailable this turn (see reply)"
    )


# ─── R2 visibility gate (FEATURE_BI_VISIBILITY_GATE, default off) ────────


def _advertised(reg: ToolRegistry, ctx: ToolExecutionContext, monkeypatch, flag: str) -> list[str]:
    from bowhead_core.capability_resolver import advertised_tool_schemas, bi_visibility_gate_enabled

    monkeypatch.setenv("FEATURE_BI_VISIBILITY_GATE", flag)
    drop_bi = bi_visibility_gate_enabled() and ctx.bi_availability() == BI_ABSENT
    whitelist = frozenset(reg.list_names())
    return [s.name for s in advertised_tool_schemas(reg, whitelist, profile_name="analyst", role=None, drop_bi=drop_bi)]


def test_visibility_gate_off_schemas_unchanged(reg: ToolRegistry, monkeypatch) -> None:
    """R6.5: flag 关（默认）→ 广告面与改动前逐字节一致（bi_* 照常在）。"""
    ctx = _ctx(datarc=object(), active=None, accessible=())  # absent
    names = _advertised(reg, ctx, monkeypatch, "0")
    assert any(n.startswith("bi_") for n in names)


def test_visibility_gate_on_absent_drops_bi(reg: ToolRegistry, monkeypatch) -> None:
    ctx = _ctx(datarc=object(), active=None, accessible=())  # absent
    names = _advertised(reg, ctx, monkeypatch, "1")
    assert names, "非 bi 工具仍应在"
    assert not any(n.startswith("bi_") for n in names)


def test_visibility_gate_on_needs_selection_keeps_bi(reg: ToolRegistry, monkeypatch) -> None:
    """R2.2: needs_selection 恒不剔——sentinel 弹卡靠工具调用触发，剔了就没人弹卡。"""
    ctx = _ctx(datarc=object(), active=None, accessible=WS2)  # needs_selection
    names = _advertised(reg, ctx, monkeypatch, "1")
    assert any(n.startswith("bi_") for n in names)


def test_visibility_gate_on_available_keeps_bi(reg: ToolRegistry, monkeypatch) -> None:
    ctx = _ctx(datarc=object(), active="42", accessible=WS2)  # available
    names = _advertised(reg, ctx, monkeypatch, "1")
    assert any(n.startswith("bi_") for n in names)


# ─── R6.4 mirrored two-site grep-gate ─────────────────────────────────


def test_turn_runner_mirror_ctx_sites_both_wired() -> None:
    """turn_runner 的两个 ToolExecutionContext 构造点是刻意 duplicate（代码注释自述
    「字段保持同步是后续维护责任」）——grep-gate 钉住新字段两处都在。"""
    src = (pathlib.Path(__file__).resolve().parents[2] / "bowhead_core" / "src" / "bowhead_core" / "turn_runner.py").read_text(encoding="utf-8")
    assert src.count('datarc_absence_reason=getattr(request, "datarc_absence_reason", "")') >= 2


@pytest.mark.parametrize("bound", [False, True], ids=["no-client", "bound-client"])
async def test_rejected_credentials_block_only_datarc_with_actionable_error(reg, bound):
    ctx = _ctx(datarc=object() if bound else None, active=None, absence_reason="connection_rejected")
    inv = await reg.execute("bi_schema", {}, ctx)
    assert inv.error and "HTTP 401/403" in inv.error
    assert "expired or was revoked" in inv.error
    assert "Team settings → Tool Sources" in inv.error
    prompt = build_lead_system_prompt("ws_x", facts=[], has_datarc=False, no_datarc_reason="connection_rejected")
    assert "expired or was revoked" in prompt
    assert "This notice applies only to Datarc" in prompt

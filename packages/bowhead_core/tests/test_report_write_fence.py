"""report-engine Phase-2 WP-F — static lint: the *legacy* canvas write tools are
never CCA-grantable. 原设计里 `write_report` 也在这道栏内；**2026-09-09 用户拍板放开**
（docs/sprints/custom-agent-report-pen-2026-09-09/tech-design.md）——它有 per-section
owner 强制（服务端盖章、LLM 伪造不了）+ rev CAS + bake，且团队 agent 取不到系统 peer 名，
`_write_guard` 的注释本来就写明它的安全模型是 owner 而非 trust class。

**仍然由本文件守住的属性**（放开的只有 `write_report` 一项）：
① 四个 legacy canvas 写工具（add_section / compose_final / patch / reorder）永不可授权
   —— 它们没有 owner 模型，运行时只有 `_write_guard` 的 trust-class 门；
② 持有它们的 system manifest 是一个**闭集**，且 trust_class 必须是写类；
③ `write_report` 的 manifest 持有者集合不因本次变化（自建 agent 另经作者面授权）。
跨轮 owner 放行仍是 system manifest 专属，测在 `test_report_tools.py`
（`test_cross_turn_relaxation_is_system_manifest_only`）。"""

from __future__ import annotations

from bowhead_core.agents import AgentRegistry
from bowhead_core.custom_subagent_store import (
    REPORT_WRITE_TOOLS,
    filter_destructive,
)

# The intended CLOSED set of system manifests allowed to hold report-write tools.
# Grows as Phase-2 adds report-writer manifests (WP-D); these tests force an
# explicit, reviewed update whenever a manifest gains a write tool — replacing
# the incomplete test_I3 (which only checked Lead + omitted add_section/compose_final).
#
# delivery-surface M-C security review (2026-08-13): grounds for expanding this set:
# ① The pen has guards: write_report embeds bake_report_markdown (M-B: stripping/[N] minting/
#   number anti-forgery/figure-anchor checks) + server-stamped owner + rev CAS; writer identity is no longer the defense.
# ② Only system manifests expand (code review surface); trust_class becomes report-writer too
#   (WP-F reserved class; see _write_guard.WRITE_TRUST_CLASSES comments).
# ③ CCA fence unchanged: filter_destructive still strips all REPORT_WRITE_TOOLS;
#   custom team subagents still cannot write (security P0 preserved, pinned by this file's first three tests).
# Narrowed on 2026-09-29: builtin canvas-writer was entirely removed in c4d50b03b (manifest deleted).
# The closed set only shrinks; removing writers narrows permissions.
_EXPECTED_WRITE_MANIFESTS = {
    "compute",
    "kb-analyst",
    "financial-analyst",
    "business-analyst",
    "compliance-analyst",
    "marketing-analyst",
}
_WRITE_TRUST_CLASSES = {"write-leaf", "report-writer"}

# Closed set of manifests holding the four legacy canvas write tools (distinct from write_report holders:
# the latter is _EXPECTED_WRITE_MANIFESTS above; multi-writer WP-D lets specialists author their own sections).
# canvas-update-turn batch B2-2 (2026-09-24, independently reviewed): compute holds `remove_chart` to collapse superseded
# prior-turn charts during follow-up revision (soft collapse, expandable). It also lacks per-section ownership, so fence it to system manifests;
# compute's report-writer trust_class passes runtime `_write_guard`. compute holds none of the other four tools.
_EXPECTED_LEGACY_WRITE_MANIFESTS = {"compute"}

# Closed set of manifests holding `write_report` (same as the writer set above after canvas-writer removal).
_EXPECTED_WRITE_REPORT_MANIFESTS = _EXPECTED_WRITE_MANIFESTS


def test_filter_destructive_keeps_only_recoverable_chart_removal() -> None:
    assert filter_destructive(sorted(REPORT_WRITE_TOOLS)) == ["remove_chart"]


def test_report_write_stripped_when_mixed_with_benign_tools() -> None:
    out = filter_destructive(
        ["bi_query", "write_report", "canvas_add_section", "skill_read", "canvas_patch_section"],
    )
    # Legacy canvas writes remain fenced (the four without an owner model)
    assert "canvas_add_section" not in out
    assert "canvas_patch_section" not in out
    # 2026-09-09: grant write_report to custom agents (the actual guard is per-section owner, not this fence)
    assert "write_report" in out
    # benign tools survive (fence is exact, not over-broad)
    assert "bi_query" in out and "skill_read" in out


def test_report_write_set_is_the_known_closed_set() -> None:
    # guard against silent additions/removals of write tools without updating the fence
    assert (
        frozenset(
            {
                # ⚠ `write_report` left the fence on 2026-09-09 (see file header). These four remain because
                # they lack per-section owner models and therefore depend on the trust-class gate.
                "canvas_add_section",
                "canvas_compose_final",
                "canvas_patch_section",
                "canvas_reorder_sections",
                # Batch B2-2: collapse prior-turn charts; also lacks an owner model.
                "remove_chart",
            }
        )
        == REPORT_WRITE_TOOLS
    )


def test_only_closed_set_of_manifests_hold_write_tools() -> None:
    """The statically-checkable security property the re-review said was lost:
    only an explicit, reviewed closed set of system manifests may hold any
    report-write tool. Fails loudly if a manifest silently gains one."""
    reg = AgentRegistry.bootstrap()
    holders = {name for name in reg.list_all() if set(reg.get(name).tools.allow) & REPORT_WRITE_TOOLS}
    assert holders == _EXPECTED_LEGACY_WRITE_MANIFESTS, (
        f"unexpected legacy-canvas-write holders: {holders ^ _EXPECTED_LEGACY_WRITE_MANIFESTS} (update only with a security review)"
    )


def test_write_report_manifest_holders_unchanged() -> None:
    """`write_report` 现在也可授权给自建 agent，但**系统 manifest 侧的持有者集合**仍应是
    那个经过评审的闭集——manifest 悄悄多一个持笔者仍要当场红。"""
    reg = AgentRegistry.bootstrap()
    holders = {name for name in reg.list_all() if "write_report" in set(reg.get(name).tools.allow)}
    assert holders == _EXPECTED_WRITE_REPORT_MANIFESTS, f"unexpected write_report holders: {holders ^ _EXPECTED_WRITE_REPORT_MANIFESTS}"


def test_write_report_is_grantable_to_custom_agents_but_legacy_writes_are_not() -> None:
    """本次放开的目标属性，正反一起断言（2026-09-09）。"""
    from bowhead_core.subagent_authoring_catalog import NATIVE_ALLOWLIST, classify_native_risk

    assert "write_report" in NATIVE_ALLOWLIST
    assert classify_native_risk("write_report") == ("artifact_write", "native_registry")
    for legacy in ("canvas_add_section", "canvas_compose_final", "canvas_patch_section", "canvas_reorder_sections", "remove_chart"):
        assert legacy not in NATIVE_ALLOWLIST, legacy


def test_write_tool_holders_are_write_leaf() -> None:
    """Every manifest holding a write tool must be a write-leaf/report-writer
    trust-class — so the trust label can't drift from the actual write surface."""
    reg = AgentRegistry.bootstrap()
    for name in reg.list_all():
        m = reg.get(name)
        if set(m.tools.allow) & (REPORT_WRITE_TOOLS | {"write_report"}):
            assert m.trust_class in _WRITE_TRUST_CLASSES, f"{name} holds a report-write tool but trust_class={m.trust_class!r}"

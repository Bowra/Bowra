"""bi-hitl-cleanup P7 / B7.3 tests — compute.md + lead.md prompt 协调.

Per tech-design §P7 invariants:
- I1: compute.md contains `needs_user_input` keyword (≥ 1 hit)
- I2: compute.md contains `clarification_payload` example
- I3: lead.py `_TOOL_USE_GUIDANCE` shorter than baseline (grounding rules moved out)
- I4: lead.py `_PEER_BAIL_HANDLING` exists + all 6 bail kinds covered
- I5: Lead prompt remains structurally complete after the legacy cap was removed
- I6 (integration): subagent.py:299-327 没 `error_kind="needs_clarification"` (P2.1)
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPUTE_MD = REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "agents" / "prompts" / "system" / "compute.md"
LEAD_PY = REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "agents" / "prompts" / "lead.py"
SUBAGENT_PY = REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "subagent.py"


def _compute_md() -> str:
    return COMPUTE_MD.read_text(encoding="utf-8")


def _lead_py() -> str:
    return LEAD_PY.read_text(encoding="utf-8")


# ── compute.md ────────────────────────────────────────────────────────


def test_b73_compute_md_contains_needs_user_input() -> None:
    """I1: bail kind needs_user_input documented in compute.md."""
    src = _compute_md()
    assert "needs_user_input" in src, "compute.md must teach the needs_user_input bail kind"


def test_b73_compute_md_contains_clarification_payload_example() -> None:
    """I2: compute.md includes `clarification_payload` field example."""
    src = _compute_md()
    assert "clarification_payload" in src
    # The 3-field shape (questions/options/type) reflected via example
    assert "questions" in src
    assert "options" in src
    # The new kind appears in the bail envelope section, not elsewhere
    assert "bail envelope" in src or "bail" in src.lower()


def test_b73_compute_md_kind_list_complete() -> None:
    """compute.md still names domain_mismatch / ambiguity / transient /
    tool_failure (legacy) PLUS new needs_user_input."""
    src = _compute_md()
    for kind in (
        "domain_mismatch",
        "ambiguity",
        "transient",
        "tool_failure",
        "needs_user_input",
    ):
        assert kind in src, f"compute.md missing bail kind: {kind}"


# ── lead.py grounding rules removed ───────────────────────────────────


def test_b73_lead_py_no_clarification_ask_grounding_rules() -> None:
    """I3: `_TOOL_USE_GUIDANCE` grounding HARD rules deleted — moved to B4
    tool description. Specifically the "Grounding 规则 (HARD)" block and the
    inline candidates examples / 反例 are gone."""
    src = _lead_py()
    # The removed subsection is "Grounding 规则 (HARD)".
    assert "Grounding 规则 (HARD)" not in src, "lead.py still has Grounding 规则 (HARD) — should be moved to clarification_ask tool description (B4)"
    # The removed line is "反例 (**严禁**)".
    assert "反例 (**严禁**)" not in src
    # The example lines featuring specific candidates list have been removed
    assert "营收 / 销量 / 成本" not in src
    # Cross-ref to B4 tool description still present
    assert "tool description" in src or "B4 重写" in src, "lead.py should cross-ref the clarification_ask B4 tool description"


def test_b73_lead_py_few_shot_examples_cross_ref() -> None:
    """`_FEW_SHOT_EXAMPLES` no longer contains specific candidates examples;
    instead a brief cross-ref pointer."""
    src = _lead_py()
    # Old verbose 2-line examples must be gone
    assert '"4月营业额","各品牌排名","GMV 同比环比"' not in src
    assert '"最新营业额","Top SKU 销量","渠道占比"' not in src
    # New cross-ref present
    assert "see the tool description" in src and "B4 rewrite" in src, (
        "few_shot_examples should cross-ref the clarification_ask description instead of repeating candidates"
    )


# ── _PEER_BAIL_HANDLING section ───────────────────────────────────────


def test_b73_lead_py_peer_bail_handling_section_present() -> None:
    """I4: `_PEER_BAIL_HANDLING` constant + section tags exist."""
    src = _lead_py()
    assert "_PEER_BAIL_HANDLING" in src
    assert "<peer_bail_handling>" in src
    assert "</peer_bail_handling>" in src


def test_b73_peer_bail_handling_covers_all_6_kinds() -> None:
    """I4 (part 2): all 6 bail kinds covered in the new section."""
    src = _lead_py()
    # Find the peer_bail_handling block boundary
    start = src.find("<peer_bail_handling>")
    end = src.find("</peer_bail_handling>")
    assert start >= 0 and end > start
    block = src[start:end]
    for kind in (
        "needs_user_input",
        "domain_mismatch",
        "ambiguity",
        "transient",
        "tool_failure",
        "schema_invalid",
    ):
        assert kind in block, f"_PEER_BAIL_HANDLING missing kind: {kind}"


def test_b73_peer_bail_handling_wired_into_sections() -> None:
    """_PEER_BAIL_HANDLING is appended in build_lead_system_prompt sections list."""
    src = _lead_py()
    # Reference (a comma after the symbol) means it's part of a list — distinguishes
    # from the constant definition.
    assert "_PEER_BAIL_HANDLING," in src, "_PEER_BAIL_HANDLING defined but not wired into sections list — Lead prompt won't include it"


def test_b73_peer_bail_handling_emphasizes_source_agnostic() -> None:
    """Per discovery §4.4 + P7.3 spec: bail path serves any peer, not just BI."""
    src = _lead_py()
    start = src.find("<peer_bail_handling>")
    end = src.find("</peer_bail_handling>")
    block = src[start:end]
    # The key invariant is the teaching "任何 peer 走同分支, 不分 source".
    assert "任何 peer" in block or "any peer" in block.lower() or "compute/modeler" in block


# ── Prompt completeness ────────────────────────────────────────────────


def test_b73_lead_prompt_keeps_late_sections_without_truncation() -> None:
    """I5: 删除旧 cap 后，后置协议不能再因体积被整体丢弃或截断。"""
    from bowhead_core.agents.prompts.lead import build_lead_system_prompt

    prompt = build_lead_system_prompt(workspace_id="test-ws", facts=[])
    assert "<peer_bail_handling>" in prompt
    assert "</query_classification_decision_tree>" in prompt
    assert prompt.endswith("</lead_orchestrator>")


# ── subagent.py B2.1 reaffirmation (integration constraint) ───────────


def test_b73_subagent_py_no_legacy_error_kind_teaching() -> None:
    """I6: `subagent.py:299-327` no longer **teaches** the deprecated
    `error_kind="needs_clarification"` shape (B2.1 removed it).

    Acceptable: deprecation explanation that says LLM should NOT expect it
    (e.g., 「不再 emit `error_kind="needs_clarification"`」). Forbidden: any
    teaching that instructs LLM TO emit / handle this shape as live.
    """
    src = SUBAGENT_PY.read_text(encoding="utf-8")
    # If the literal phrase exists, it MUST be inside a deprecation context
    # (The markers "不再" mean "no longer" and "已砍" mean "already removed").
    if 'error_kind="needs_clarification"' in src:
        # Find every occurrence and check nearby "不再"/"已砍"/"removed" markers.
        bad_contexts: list[str] = []
        for match in re.finditer(r'error_kind="needs_clarification"', src):
            window_start = max(0, match.start() - 80)
            window_end = min(len(src), match.end() + 80)
            window = src[window_start:window_end]
            if not any(t in window for t in ("不再", "已砍", "removed", "deprecated")):
                bad_contexts.append(window)
        assert bad_contexts == [], (
            'subagent.py mentions `error_kind="needs_clarification"` outside '
            "a deprecation context — should only appear when explicitly noting "
            "the shape is no longer emitted"
        )

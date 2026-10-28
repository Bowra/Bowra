"""Sprint ω Phase 3 — skill catalog block invariants.

Tests r3.1 §2.10 spec:
- Each skill description ≤ 80 tokens (default) or ≤ 200 tokens (description_long)
- Whole catalog block ≤ 2000 tokens per role (only "lead" today)
- Deterministic-sorted (byte-identical across calls; DeepSeek prefix-cache invariant)
- Lead prompt embed flag-gated via BOWHEAD_OMEGA_SKILLS_CATALOG (default ON)
- description_long: true frontmatter unlocks 300-char cap for 2 long-form skills
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from bowhead_core.agents.prompts.lead import (
    _reset_skill_catalog_block_cache_for_test,
    build_lead_system_prompt,
)
from bowhead_core.token_estimator import estimate_tokens

from bowhead_skill import default_catalog, render_index

_DEFAULT_CHAR_CAP = 160
_LONG_CHAR_CAP = 300
_DEFAULT_TOK_CAP = 80
_LONG_TOK_CAP = 200
_CATALOG_TOTAL_TOK_CAP = 2000

# G4 (2026-07-03, replaces ROUTE-5 hardcoded prefixes): filter Lead's catalog by skill frontmatter
# `audience: peer` (analyst-method-*/analyst-sop-*/python_sandbox
# and other peer execution details stay out of Lead's prompt). Budget invariants apply only to
# entries actually appearing in Lead's prompt; rich peer-only skill descriptions
# (>80 tokens) are intentional: help peers select methods via skill_list without consuming Lead's budget.


def _lead_block_specs(cat):
    """Specs that actually render into the Lead catalog block (audience!=peer)."""
    return [s for s in cat.list_specs() if getattr(s, "audience", "all") != "peer"]


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    """Reset lazy catalog block cache + cl100k encoder LRU between tests."""
    _reset_skill_catalog_block_cache_for_test()
    yield
    _reset_skill_catalog_block_cache_for_test()


# ─── Per-skill description budget (r3.1 §2.10) ───────────────────────────


def test_every_skill_description_within_token_cap() -> None:
    """Each LEAD-block skill desc ≤ 80 tokens (default) or ≤ 200 (description_long).
    Peer-internal method/SOP skills are excluded (ROUTE-5) — they don't appear in
    the Lead prompt and their richer descriptions aid peer skill_list selection."""
    cat = default_catalog()
    cat.refresh()
    fails: list[str] = []
    for spec in _lead_block_specs(cat):
        is_long = bool(spec.frontmatter.get("description_long", False))
        cap = _LONG_TOK_CAP if is_long else _DEFAULT_TOK_CAP
        n = estimate_tokens(spec.description)
        if n > cap:
            fails.append(f"{spec.name}: {n} tok > {cap} cap (long={is_long})")
    assert fails == [], f"Lead-block skills over token cap: {fails}"


def test_every_skill_description_within_char_cap() -> None:
    """Each LEAD-block skill desc ≤ 160 chars (default) or ≤ 300 (description_long).
    Peer-internal method/SOP skills excluded (ROUTE-5; see token-cap test)."""
    cat = default_catalog()
    cat.refresh()
    fails: list[str] = []
    for spec in _lead_block_specs(cat):
        is_long = bool(spec.frontmatter.get("description_long", False))
        cap = _LONG_CHAR_CAP if is_long else _DEFAULT_CHAR_CAP
        n = len(spec.description)
        if n > cap:
            fails.append(f"{spec.name}: {n} chars > {cap} cap (long={is_long})")
    assert fails == [], f"Lead-block skills over char cap: {fails}"


def test_description_long_flag_only_on_known_long_skills() -> None:
    """r3.1 §2.10 pinned 2 long-form exceptions: analyst-framework, xlsx.
    Adding more requires PRD update — guard against accidental flag bloat."""
    cat = default_catalog()
    cat.refresh()
    long_skills = {s.name for s in cat.list_specs() if s.frontmatter.get("description_long", False)}
    expected = {"analyst-framework", "xlsx"}
    assert long_skills == expected, f"description_long flag drift: expected {expected}, got {long_skills}"


# ─── Whole catalog budget ────────────────────────────────────────────────


def test_catalog_block_within_total_budget() -> None:
    """The catalog block AS RENDERED INTO THE LEAD PROMPT ≤ 2000 tokens — leaves
    room for facts/prompt sections. G4: the Lead block excludes audience:peer
    skills (see skill_access.render_lead_skill_index), so the budget is measured
    against the same view the Lead prompt uses."""
    cat = default_catalog()
    cat.refresh()
    block = render_index(cat, audience_view="lead")
    n = estimate_tokens(block)
    assert n <= _CATALOG_TOTAL_TOK_CAP, (
        f"Lead catalog block {n} tok > {_CATALOG_TOTAL_TOK_CAP} cap. Trim skill descriptions or flip more to description_long."
    )


# ─── Deterministic sort (DeepSeek prefix-cache invariant per Spike Q5) ───


def test_catalog_block_deterministic_across_calls() -> None:
    """Same catalog → byte-identical render output. Required for DeepSeek
    prefix-cache stability (Q5 finding: 85-90% hit if prefix identical)."""
    cat = default_catalog()
    cat.refresh()
    a = render_index(cat)
    b = render_index(cat)
    assert a == b, "render_index must be deterministic"


def test_catalog_block_alphabetical_order() -> None:
    """Skills appear in alphabetical-by-name order (sorted via list_names)."""
    cat = default_catalog()
    cat.refresh()
    block = render_index(cat)
    # Extract `**name**` entries in order
    names_in_block = re.findall(r"^\- \*\*([^*]+)\*\*", block, re.MULTILINE)
    assert names_in_block == sorted(names_in_block), f"Skill order in catalog must be alphabetical. Got: {names_in_block}"


# ─── Lead prompt wire (flag-gated, default ON) ───────────────────────────


def test_lead_prompt_includes_catalog_when_flag_default_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-05-12 ω 0.13.1 re-flip: BOWHEAD_OMEGA_SKILLS_CATALOG default ON.
    一度 OFF 因 dogfood metric script bug (omicron-12 tool name dot→underscore
    rename 后 `prod_dogfood.py` 数 0). 真实 server log: peer dispatch 一直正常.
    Catalog ON 不 hijack Lead. Unset env = block IN."""
    monkeypatch.delenv("BOWHEAD_OMEGA_SKILLS_CATALOG", raising=False)
    _reset_skill_catalog_block_cache_for_test()
    prompt = build_lead_system_prompt(
        workspace_id="ws_test",
        facts=[],
    )
    assert "## Available Skills" in prompt
    assert "analyst-framework" in prompt
    # G4: Lead's view must exclude peer-only entries and the old misleading header teaching Lead to call skill_read/invoke.
    assert "**python_sandbox**" not in prompt
    # Batch 3 removed bi-concept-resolve, making this assertion trivially true (a nonexistent skill cannot appear in the prompt);
    # replace it with a peer-only skill **that actually exists today**, otherwise this is a dead assertion
    # （`feedback_assert_absence_needs_positive_control`）。
    assert "**analyst-sop-diagnosis**" not in prompt
    assert "skill_invoke(name, script, args)" not in prompt
    assert "subagent_spawn" in prompt  # Routing-semantic header points to the correct action
    # The method/SOP destination summary line remains.
    assert "analyst-method-\\*" in prompt


def test_lead_prompt_omits_catalog_when_flag_explicit_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit OFF still works — for emergency rollback experiments."""
    monkeypatch.setenv("BOWHEAD_OMEGA_SKILLS_CATALOG", "0")
    _reset_skill_catalog_block_cache_for_test()
    prompt = build_lead_system_prompt(
        workspace_id="ws_test",
        facts=[],
    )
    assert "## Available Skills" not in prompt


def test_lead_prompt_flag_flip_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r3.1 invariant: env read at function-call time, NOT module top."""
    monkeypatch.setenv("BOWHEAD_OMEGA_SKILLS_CATALOG", "0")
    _reset_skill_catalog_block_cache_for_test()
    p_off = build_lead_system_prompt(
        workspace_id="ws_test",
        facts=[],
    )
    assert "## Available Skills" not in p_off

    monkeypatch.setenv("BOWHEAD_OMEGA_SKILLS_CATALOG", "1")
    # Cache helper short-circuits — but flag is checked at function call,
    # so output should reflect the flip.
    p_on = build_lead_system_prompt(
        workspace_id="ws_test",
        facts=[],
    )
    assert "## Available Skills" in p_on


def test_catalog_block_build_failure_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构建失败必须返空但不缓存——一次瞬时故障不该让 Lead 到进程重启前都无索引（audit #16）."""
    import bowhead_core.skill_access as skill_access
    from bowhead_core.agents.prompts.lead import _get_skill_catalog_block

    calls = {"n": 0}
    real = skill_access.render_lead_skill_index

    def _flaky(cat):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient boot race")
        return real(cat)

    monkeypatch.setattr(skill_access, "render_lead_skill_index", _flaky)
    assert _get_skill_catalog_block() == ""
    # The second call must retry and succeed instead of permanently returning a cached empty string.
    recovered = _get_skill_catalog_block()
    assert "analyst-framework" in recovered
    assert calls["n"] == 2


# ─── description_long YAML frontmatter present in 2 expected skills ──────


def _catalog_md_path(skill_name: str) -> Path:
    # __file__: .../packages/bowhead_core/tests/test_skill_catalog_block.py
    # parents[2]: .../packages/   ;  sibling package: bowhead_skill/catalog
    return Path(__file__).resolve().parents[2] / "bowhead_skill/catalog" / skill_name / "SKILL.md"


def test_analyst_framework_has_description_long_flag() -> None:
    p = _catalog_md_path("analyst-framework")
    raw = p.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.+?)\n---\n", raw, re.DOTALL)
    assert m, f"frontmatter not parseable: {p}"
    fm = yaml.safe_load(m.group(1))
    assert fm.get("description_long") is True


def test_xlsx_has_description_long_flag() -> None:
    p = _catalog_md_path("xlsx")
    raw = p.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.+?)\n---\n", raw, re.DOTALL)
    assert m, f"frontmatter not parseable: {p}"
    fm = yaml.safe_load(m.group(1))
    assert fm.get("description_long") is True

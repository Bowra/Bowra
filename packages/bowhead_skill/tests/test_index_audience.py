"""audience frontmatter + render_index 分视图（G4，docs/skills-tools-ssot.md §5-G4）.

- loader：`audience: peer|all`（默认 all，未知值容错回 all）
- render_index(audience_view="lead")：过滤 audience:peer 条目 + 路由语义 header
  （不教 Lead 调它没有的 skill_read/skill_invoke）
- render_index() 默认 peer 视图：全量 + 原 header（peer 有这两个工具）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_skill import load_skill, render_index
from bowhead_skill.catalog import SkillCatalog, builtin_catalog_dir

if TYPE_CHECKING:
    from pathlib import Path


def _write_skill(root: Path, name: str, fm_extra: str = "") -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc of {name}\n{fm_extra}---\nbody\n",
        encoding="utf-8",
    )


def test_loader_audience_default_and_peer(tmp_path: Path) -> None:
    _write_skill(tmp_path, "plain-skill")
    _write_skill(tmp_path, "peer-skill", "audience: peer\n")
    _write_skill(tmp_path, "weird-skill", "audience: nonsense\n")
    assert load_skill(tmp_path / "plain-skill").audience == "all"
    assert load_skill(tmp_path / "peer-skill").audience == "peer"
    # Unknown values fall back to all (the loader is lenient; strict validation belongs to the upload validator).
    assert load_skill(tmp_path / "weird-skill").audience == "all"


def test_render_index_lead_view_filters_and_reframes(tmp_path: Path) -> None:
    from bowhead_skill.catalog import SkillCatalog

    _write_skill(tmp_path, "lead-visible")
    _write_skill(tmp_path, "peer-only", "audience: peer\n")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()

    lead = render_index(cat, audience_view="lead")
    assert "**lead-visible**" in lead
    assert "peer-only" not in lead
    assert "subagent_spawn" in lead  # Routing semantics.
    assert "do not call them directly" in lead
    assert "skill_invoke(name, script, args)" not in lead  # The old misleading header is absent from the Lead view.

    peer = render_index(cat)  # Default peer view.
    assert "**lead-visible**" in peer and "**peer-only**" in peer
    assert "skill_read(name)" in peer  # The peer actually has this tool, so retain its header.


def test_builtin_catalog_peer_only_set_pinned() -> None:
    """内置 catalog 的 audience:peer 集合 = 11 method + 6 sop + python_sandbox
    （18 个）——防无意增删（改集合须同步 SSOT §5-G4）.

    The system-agent skills added on 2026-10-02/03 are peer-only by design: the
    Configuration Agent's `settings-*` domain skills (4aac905ba, c493fc9ef, 3e922b6aa)
    and the coach's `agent-authoring` (5801c95e8). They bring the pinned set to 24.
    The Shopify plugin Skills (`shopify-analysis` 4bf720ce2, `shopify-content-optimization`
    85b68da2a) are peer-only by design as well, bringing the pinned set to 26; `settings-space-instructions` (c058b8843) makes it 27.

    批 3（compute-agent-optimize，2026-08-20）删掉 `bi-concept-resolve`：它的名字在批 1 之前
    一直明写在 compute.md 里，**有名字的整整一个月依然 0 读**（116 30 天 `skill_read` 聚合），
    属于"被试过、没人用"。工具 `bi_concept_resolve` 不受影响（代码注册，不是 skill）。

    review-F2 fix: 用 builtin-only root（sibling test_namespace_disjoint 同法），不用
    default_catalog()——后者叠 ~/.bowhead/skills + cwd/.bowhead/skills，box 上若有本地
    audience:peer skill 会让 len==19 spurious fail（且可能掩盖真的 builtin drift）。"""
    cat = SkillCatalog(roots=[builtin_catalog_dir()])
    cat.refresh()
    peer_only = {s.name for s in cat.list_specs() if s.audience == "peer"}
    system_agent_skills = {
        "agent-authoring",
        "settings-configuration",
        "settings-language-preferences",
        "settings-personal-appearance",
        "settings-space-instructions",
        "settings-space-metadata",
        "settings-space-preferences",
    }
    plugin_skills = {"shopify-analysis", "shopify-content-optimization"}
    assert len(peer_only) == 27, sorted(peer_only)
    assert "python_sandbox" in peer_only
    assert system_agent_skills <= peer_only, sorted(system_agent_skills - peer_only)
    assert plugin_skills <= peer_only, sorted(plugin_skills - peer_only)
    # After batch 3 removed bi-concept-resolve, its absence is tautological; len==27 above guards reintroduction. Check the set shape here.
    assert "analyst-framework" not in peer_only  # Lead routing signal; keep visible.
    analyst_skills = peer_only - {"python_sandbox"} - system_agent_skills - plugin_skills
    assert len(analyst_skills) == 17, sorted(analyst_skills)
    assert all(n.startswith(("analyst-method-", "analyst-sop-")) for n in analyst_skills)

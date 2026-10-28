"""compute-agent-optimize 批1 —— 「引用 ⊆ 本 profile allow」守卫。

姊妹守卫 `test_no_hidden_tool_refs` 只抓 **全局隐藏**（`visible_to_llm=False`）的工具名；
抓不到「工具是可见的、只是不在**这个** peer 的 allow 里」这一维——而 discovery 抓到的
真机矛盾恰恰是这一类（`analyst-sop-output-contract` 教 compute 走 `canvas_add_insight`
chunk 路，compute.yaml 从没 allow 过它）。模型照做的结果不是报错，是**一整轮 LLM 白烧**。

边界：本守卫只抓「**已注册**但不在本 profile allow」；全仓未注册的幻影名（如语言 directive 曾教的
`search_dimension_value`）它认不出来，靠各自的单测钉（test_language_directive）。工具 schema 描述与
校验拒收文案也不在扫描面（它们是 LLM 可见面，但跨 peer 共享，措辞上避免点名即可）。

守卫口径：compute 真读得到的文本面（compute.md / compute.yaml `system.append` /
起手指引点名的 skill 族 / 语言 directive）里出现的任何**已注册**工具名，必须在
compute.yaml `tools.allow` 里。否定式提及（"你没有 X"）也算引用——写给模型看的文本
不该点一个它拿不到的工具名，改说"这类工具不对你开放"。
"""

from __future__ import annotations

import re
from pathlib import Path

import bowhead_core.agents.prompts as _prompts_pkg
from bowhead_core.agents.bi_concept_resolve import register_bi_concept_resolve_tool
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.agents.report_tools import register_report_tools
from bowhead_core.canvas_tools import register_canvas_tools
from bowhead_core.language_context import LanguageContext
from bowhead_core.language_directive import build_language_directive
from bowhead_core.memory_tools import register_memory_tools
from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools
from bowhead_core.subagent import register_subagent_tool
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_registry import build_default_registry
from bowhead_core.web_fetch import register_web_fetch_tool
from bowhead_core.web_search import register_web_search_tool

from bowhead_skill.catalog import builtin_catalog_dir

# Skill families named by compute.md's initial guidance / read on demand via skill_read: those the model actually sees.
# Specialist peers primarily read analyst-method-* with a different tool surface; exclude them from this guard.
_COMPUTE_SKILLS = (
    "analyst-framework",
    "analyst-sop-compute",
    "analyst-sop-diagnosis",
    "analyst-sop-period-compare",
    "analyst-sop-growth-driver",
    "analyst-sop-business-review",
    "analyst-sop-output-contract",
    "python_sandbox",
)

# A few locations legitimately name tools outside allow (state the reason explicitly).
_EXEMPT: dict[tuple[str, str], str] = {
    # This only describes **Lead-side** use of the needs_user_input envelope to call clarification_ask (the peer uses the envelope).
    # After batch 3, compute.md no longer mentions clarification_ask, so that exemption was removed too;
    # see test_no_dead_exemptions below: retaining it would preapprove reintroducing the reference later.
    ("analyst-framework/SKILL.md", "clarification_ask"): "描述 Lead 侧动作, peer 走 needs_user_input 信封",
}


def _all_registered_tool_names() -> frozenset[str]:
    """按 gateway 装配同序注册一遍, 取可见工具名全集（registry 是唯一真源）。"""
    reg = build_default_registry()
    cat, rt = build_default_skill_stack()
    register_skill_tools(reg, cat, rt)
    register_subagent_tool(reg)
    register_subagent_return_tool(reg)
    register_canvas_tools(reg)
    register_clarification_tool(reg)
    register_memory_tools(reg)
    register_bi_concept_resolve_tool(reg)

    async def _resolver(team_id: str):  # noqa: ANN202 — only to enable web_search registration
        return None

    register_web_search_tool(reg, team_search_resolver=_resolver)
    register_web_fetch_tool(reg)
    register_report_tools(reg)
    # Recognize only multi-token identifier names, avoiding prose false positives for ordinary words such as remember/recall.
    return frozenset(n for n, spec in reg._specs.items() if spec.visible_to_llm and ("_" in n or "." in n))


def _compute_allow() -> frozenset[str]:
    reg = AgentRegistry.bootstrap()
    return frozenset(reg.get("compute").tools.allow)


def _compute_texts() -> list[tuple[str, str]]:
    prompts_dir = Path(_prompts_pkg.__file__).resolve().parent  # type: ignore[arg-type]
    out: list[tuple[str, str]] = []
    compute_md = prompts_dir / "system" / "compute.md"
    assert compute_md.is_file(), compute_md
    out.append(("compute.md", compute_md.read_text(encoding="utf-8")))
    from bowhead_core.agents.prompts.simple_lookup_locale import load_lookup_templates

    for locale in ("en-US", "zh-CN", "de-DE", "fr-FR"):
        rules = load_lookup_templates(locale)
        out.append((f"simple-lookup/{locale}", "\n".join(text for key, text in rules.items() if not key.startswith("lead_"))))
    manifest = AgentRegistry.bootstrap().get("compute")
    assert manifest.system is not None
    out.append(("compute.yaml:system.append", manifest.system.append or ""))
    catalog = builtin_catalog_dir()
    for name in _COMPUTE_SKILLS:
        d = catalog / name
        assert d.is_dir(), d
        for md in sorted(d.rglob("*.md")):
            out.append((f"{name}/{md.relative_to(d)}", md.read_text(encoding="utf-8")))
    out.append(
        (
            "language_directive(compute)",
            build_language_directive(LanguageContext.default(), "compute", catalog=None, include_tool_constraint=True),
        )
    )
    return out


def test_compute_allow_is_nonempty_and_visible() -> None:
    allow = _compute_allow()
    assert "bi_query" in allow and "subagent_return" in allow
    # Batch 1: hidden bi_fetch_rows must no longer appear in allow (bi_query calls it internally; keeping it misleads guards and readers).
    assert "bi_fetch_rows" not in allow


def _find_disallowed(
    texts: list[tuple[str, str]],
    registered: frozenset[str],
    allow: frozenset[str],
    exempt: dict[tuple[str, str], str],
) -> list[str]:
    candidates = sorted(registered - allow, key=len, reverse=True)
    assert candidates, "没有任何「已注册但不在 compute allow」的工具——守卫会空转"
    pattern = re.compile(r"(?<![A-Za-z0-9_.])(" + "|".join(re.escape(n) for n in candidates) + r")(?![A-Za-z0-9_])")
    hits: list[str] = []
    for label, text in texts:
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in pattern.finditer(line):
                tool = m.group(1)
                if (label, tool) in exempt:
                    continue
                hits.append(f"{label}:{lineno}: `{tool}` 不在 compute.yaml tools.allow — {line.strip()[:100]}")
    return hits


def test_guard_positive_control_catches_the_original_contract_bug() -> None:
    """摘掉守卫仍绿 = 守卫是死的。把 discovery 抓到的原话喂进去，必须红。"""
    registered = _all_registered_tool_names()
    allow = _compute_allow()
    assert "canvas_add_insight" in registered and "canvas_add_insight" not in allow
    bad = [("fake/SKILL.md", "insights 数 ≥ 3 时必走 `canvas_add_insight` 一段一段调")]
    hits = _find_disallowed(bad, registered, allow, {})
    assert len(hits) == 1 and "canvas_add_insight" in hits[0]
    # Negative control: tool names in allow are not flagged.
    ok = [("fake/SKILL.md", "先 `add_chart` 再 `subagent_return`")]
    assert _find_disallowed(ok, registered, allow, {}) == []


def test_no_dead_exemptions() -> None:
    """每条 exemption 都必须**真的被用到**。

    没人命中的 exemption 不是「无害残留」——它是给「以后有人把这个工具名写回 compute
    读得到的文本里」预先开的绿灯，而那正是本守卫存在的理由。批 3 逮到过一条：compute.md
    重写后不再提 `clarification_ask`，exemption 却还留着。
    """
    texts = _compute_texts()
    registered = _all_registered_tool_names()
    allow = _compute_allow()
    # Use **the guard's own decision path**: an exemption is live iff removing it actually adds a hit.
    # (Text grep alone is insufficient: once a tool enters allow, `_find_disallowed` never consults the exemption,
    # so it permanently becomes ineffective although the string remains in the text.)
    base = set(_find_disallowed(texts, registered, allow, _EXEMPT))
    unused: list[str] = []
    for key in _EXEMPT:
        without = {k: v for k, v in _EXEMPT.items() if k != key}
        if not (set(_find_disallowed(texts, registered, allow, without)) - base):
            unused.append(f"{key[0]} / {key[1]}")
    assert not unused, "exemption 摘掉也不会多出命中 = 它是死的（要么文本改了、要么工具进了 allow）:\n" + "\n".join(unused)


def test_every_tool_named_in_compute_facing_text_is_in_compute_allow() -> None:
    hits = _find_disallowed(_compute_texts(), _all_registered_tool_names(), _compute_allow(), _EXEMPT)
    assert not hits, "compute 读得到的文本点名了它调不到的工具（模型照做 = 一整轮白烧）:\n" + "\n".join(hits)

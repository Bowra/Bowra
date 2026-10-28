"""Render fetch rules from capable peers in the current roster.

Rule names must identify spawnable peers. Disabling a builtin is configuration,
not an outage, when a custom Agent provides the capability.
"""

from __future__ import annotations

from bowhead_core.agents.prompts import peer_block
from bowhead_core.agents.prompts.peer_block import (
    DEFAULT_DATA_PEER,
    PeerEntry,
    pick_data_peer,
    render_peer_block,
)

# WP M3-12: build fixtures in **production shape**: `_gather_lead_peers` fills
# `tool_names` and `primary_capability` (contract declaration) from the manifest. Earlier fixtures supplied only `can_fetch_bi`,
# the shape before the criterion changed; retaining it would miss actual post resolution.
_COMPUTE = PeerEntry(
    "compute", "数据分析 peer", "system", can_fetch_bi=True, tool_names=frozenset({"bi_query", "python_exec"}), primary_capability="cap_data_query"
)
_KB = PeerEntry("kb-analyst", "KB 检索", "system", tool_names=frozenset({"kb_search"}), primary_capability="cap_knowledge_search")
# Synthetic custom Agent with bi_query and no declared capability contract.
_CUSTOM_DATA = PeerEntry("regional-data-analyst", "Regional Sales Analyst", "team", can_fetch_bi=True, tool_names=frozenset({"bi_query"}))

# WP M3-12 batch 2: the other four posts also follow roster rendering, so "byte-for-byte unchanged" holds only when **every post has
# its historical holder**. A roster without a deck / canvas-editing holder renders "there is currently no Agent
# able to do this" — **correct** new behavior, not a regression.
_CONSULTANT = PeerEntry(
    "consultant", "商业顾问", "system", tool_names=frozenset({"skill_invoke"}), primary_capability="cap_presentation", output_formats=("pptx",)
)
_CANVAS = PeerEntry("canvas-writer", "画布编辑", "system", tool_names=frozenset({"canvas_patch_section"}), primary_capability="cap_document")
_MARKETING = PeerEntry("marketing-analyst", "文生图", "system", tool_names=frozenset({"image_generate"}), primary_capability="cap_visualize")
_ALL_DEFAULT = [_COMPUTE, _KB, _CANVAS, _CONSULTANT, _MARKETING]

# Outage wording: none may appear in the disabled notice.
_OUTAGE_WORDS = ("不可用", "未恢复", "稍后重试", "服务故障")


# ─────────── Choosing the data-fetching peer ───────────


def test_compute_in_roster_wins_unchanged() -> None:
    """回滚绳：compute 在场就是它，绝大多数团队走这条。"""
    assert pick_data_peer([_COMPUTE, _KB, _CUSTOM_DATA]) == DEFAULT_DATA_PEER


def test_falls_to_the_bi_capable_peer_when_compute_is_gone() -> None:
    """Use a capable custom peer when compute is absent from the roster."""
    assert pick_data_peer([_KB, _CUSTOM_DATA]) == "regional-data-analyst"


def test_a_peer_without_bi_tools_is_never_picked() -> None:
    """`can_fetch_bi=False` 的 peer 不许冒充取数 peer——把取数派给一个没有
    bi_query 的 agent，比说"没有能力"更糟：它会去编。"""
    assert pick_data_peer([_KB]) is None


def test_a_builtin_specialist_never_inherits_the_data_peer_role() -> None:
    """★ 生产形态里 kb-analyst / consultant / 三个 specialist 的白名单**都有**
    `bi_query`（manifest 覆盖 PROFILES 之后）。但"手上有取数工具"≠"职责是取数"：
    把「取数一律 spawn X」的 X 换成 kb-analyst，等于把 BI 请求派给做文档研究的 agent。

    这条是我第一版判据的反例——当时按"有 bi_query"选，在未 bootstrap 的 fallback
    dict 上看着对（那里只有 compute），生产里会选中 kb-analyst。
    """
    kb_with_bi = PeerEntry(
        "kb-analyst",
        "KB 检索",
        "system",
        can_fetch_bi=True,
        tool_names=frozenset({"bi_query", "kb_search"}),
        primary_capability="cap_knowledge_search",
    )
    consultant = PeerEntry(
        "consultant", "商业顾问", "system", can_fetch_bi=True, tool_names=frozenset({"bi_query"}), primary_capability="cap_presentation"
    )
    assert pick_data_peer([kb_with_bi, consultant], disabled={"compute"}) is None
    # Only a replacement declared by the team can fill in.
    assert pick_data_peer([kb_with_bi, consultant, _CUSTOM_DATA], disabled={"compute"}) == "regional-data-analyst"


def test_disabled_name_never_leaks_into_the_rule() -> None:
    """防呆：即便 compute 因为别的原因还留在 peers 里，停用了就不能被选中。
    "规则点名了一个 spawn 不出来的 peer" 正是本次事故的形状。"""
    assert pick_data_peer([_COMPUTE, _CUSTOM_DATA], disabled={"compute"}) == "regional-data-analyst"


# ─────────── Rendered rules ───────────


def test_all_default_roster_renders_byte_identical_preamble() -> None:
    """回滚绳：**每个岗位都是历史承担者**时，preamble 必须与改动前逐字节一致。

    批 2 起判定从「compute 在不在」扩到「五个岗位是不是都还是默认承担者」——
    少一个 deck / 画布编辑承担者，散文本就该改口（说没有），那不是回退。
    """
    block = render_peer_block(_ALL_DEFAULT)
    assert peer_block._PEER_BLOCK_PREAMBLE in block


def test_roster_missing_a_role_holder_says_so_instead_of_naming_nobody() -> None:
    """只有取数 + KB 的花名册：deck 那句不该点一个不在场的名字。"""
    block = render_peer_block([_COMPUTE, _KB])
    assert "consultant" not in block
    assert "no Agent for new presentations" in block


def test_custom_peer_shape_names_the_real_peer_and_never_compute() -> None:
    """A disabled builtin is not named in the custom peer's fetch instructions."""
    block = render_peer_block([_KB, _CUSTOM_DATA], disabled_builtins=frozenset({"compute"}))
    assert "All data fetching goes to spawn regional-data-analyst" in block
    assert "all data is fetched by regional-data-analyst" in block
    # Named peers in report / export / single-topic rules must follow too, or another Lead phrasing still leaves an unfilled post.
    # ⚠ Since batch 2, "the first report always goes to X" appears only **with a canvas-editing holder** (it means
    # "do not assign it to the canvas-editing peer", which provides no contrast if absent). This roster has none, so assert instead
    # that a Power BI-only / data-only replacement cannot be drafted for Office exports; without a compute
    # holder, explicitly show the configuration gap.
    assert "The ordinary PDF / Excel (xlsx) / Word (docx) export post currently has no holder" in block
    assert "compute" not in block


def test_no_data_peer_says_config_not_outage() -> None:
    """一个能取数的 peer 都没有：如实说"本团队未启用"，**不许**说成故障，
    也不许点 compute 的名。"""
    block = render_peer_block([_KB], disabled_builtins=frozenset({"compute"}))
    assert "this team has not enabled data querying yet" in block
    assert "compute" not in block
    # The notice itself forbids outage wording; here only check that "try again later" is not offered as advice.
    assert "try again later" not in block.replace('"capability unavailable / service not restored / try again later"', "")
    assert "稍后重试" not in block.replace("「能力不可用 / 服务未恢复 / 稍后重试」", "")


# ─────────── Mutation gate ───────────


def test_mutation_hardcoding_compute_back_turns_red() -> None:
    """The regression guard rejects a rule that hardcodes the disabled builtin.

    Construct the old shape locally as a positive control for that boundary.
    """
    legacy_rule = peer_block.render_bi_delegation_rule(DEFAULT_DATA_PEER)
    assert "All data fetching goes to spawn compute" in legacy_rule  # Legacy text looked like this.
    custom_rule = peer_block.render_bi_delegation_rule("regional-data-analyst")
    assert "compute" not in custom_rule
    assert legacy_rule != custom_rule


def test_an_undeclared_builtin_is_never_drafted_as_the_data_peer() -> None:
    """★ 上一条靠「它声明了别的契约」挡住；这条挡的是**漏配**。

    新增一个内置却忘了写 `primary_capability` 时，它会既没认领岗位、又不该被征用 ——
    没有这道闸，一个忘了声明的内置就能被抓去当取数 peer，而生产形态下几乎每个内置的
    白名单里都有 `bi_query`。内置是策展过的，缺声明是漏配，不是「随便派我」。
    """
    undeclared_builtin = PeerEntry("some-new-builtin", "新内置", "system", tool_names=frozenset({"bi_query"}))
    assert pick_data_peer([undeclared_builtin], disabled={"compute"}) is None
    # A similarly undeclared **team-created** Agent can fill in (custom Agents commonly lack declarations).
    undeclared_team = PeerEntry("team-agent", "团队自建", "team", tool_names=frozenset({"bi_query"}))
    assert pick_data_peer([undeclared_builtin, undeclared_team], disabled={"compute"}) == "team-agent"


def test_power_bi_only_agent_is_recognised_as_the_data_peer() -> None:
    """★ 旧判据 `BI_FETCH_TOOL_NAMES` 不含 `teammcp.ibt_powerbi.pbi_execute_dax`：配了 Power BI 的团队
    自建取数 Agent 会被静默判成"不能取数"，停用 compute 后 Lead 被告知「本团队没有
    具备 BI 取数能力的 peer」——而它其实有。能力目录里 `cap_data_query` 认这个工具。
    """
    pbi_agent = PeerEntry("pbi-agent", "Power BI 分析", "team", tool_names=frozenset({"teammcp.ibt_powerbi.pbi_execute_dax"}))
    assert pick_data_peer([pbi_agent], disabled={"compute"}) == "pbi-agent"

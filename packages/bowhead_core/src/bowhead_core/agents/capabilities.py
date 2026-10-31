"""Agent 能力 —— A 轴「数据来源」（第 1 期）+ B 轴「交付物」（第 2a 期）。

**路由要对的是需求，不是名字。**

在此之前，「谁能读上传文档」「谁能联网」这两个事实被按**名字**硬编码在三处
（planner SYSTEM_PROMPT 的必须/严禁措辞、`route_step_agent` 的 `return "compute"`、
Lead prompt 的散文）。在「团队可自建几百个 agent」的前提下这必然失效：
一个团队新建的 `market-researcher` 哪怕 manifest 里明明白白挂着 `web_search`，
也永远轮不到它——代码里写死了「唯一挂 web_search 的是 compute」。

本模块把那个事实变成**从 manifest 派生的数据**：单一真源是 `tools.allow`
（deny-by-default 的显式 allowlist，system 与 user-defined 同一个 schema）。

⚠ **A 轴只解得了一半**（discovery §2.3，本 sprint 最反直觉的一条）：`consultant` 与
`financial-analyst` 的 `allow` **一字不差**，两者差别 100% 在 description 与 system
prompt 的散文里。所以「能不能出 PPT」这类**交付物**能力**推导不出来**——`skill_invoke`
是通用分发器，拥有它不代表能出 deck；而 manifest 的 `skills:` 块注释明写
`DEPRECATED-INERT`，从来没人消费（实测九个 system manifest 全是 `skills: None`）。

⇒ B 轴走**声明**：`AgentManifest.produces`（第 2a 期加）。
**别试图在本模块里用工具名去猜交付物能力** —— 那正是 §2.3 证伪掉的路。
两轴刻意分成两张表，理由见 `produces_by_agent`。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from bowhead_core.agents._generated.tool_renames import canonical_tool_name

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_LABELS_ZH",
    "QUERIES_BI",
    "READS_UPLOADS",
    "SEARCHES_WEB",
    "capabilities_by_agent",
    "derive_capabilities",
    "produces_by_agent",
    "render_capabilities",
    "render_produces",
    "required_capabilities",
    "select_capable_agent",
]

READS_UPLOADS = "reads_uploads"
"""读得了用户上传的文档 —— 判据是挂了任一 `kb_*` 工具。"""

SEARCHES_WEB = "searches_web"
"""联得了网 —— 判据是挂了 `web_search`。"""

QUERIES_BI = "queries_bi"
"""查得了本空间 BI 数仓 —— 判据复用 Capability catalog 的 data-query 证据。"""

CAPABILITIES = (READS_UPLOADS, SEARCHES_WEB, QUERIES_BI)

# Chinese labels for the planner prompt. **Not UI copy** (no i18n): the whole planner prompt is Chinese,
# so labels follow it for readability. Fixed order gives byte-identical rosters each time (prompt-cache friendly).
CAPABILITY_LABELS_ZH = {
    READS_UPLOADS: "读上传文档",
    SEARCHES_WEB: "联网检索",
    QUERIES_BI: "查本空间BI",
}

# Only declared retrieval Tools prove document-reading capability. Knowledge
# management Tools mutate membership and do not grant retrieval access.


def derive_capabilities(tool_names: Iterable[str] | None) -> frozenset[str]:
    """从一个 agent 的 `tools.allow` 派生能力标签集合。

    纯函数、无 I/O —— 这样 planner / 执行侧 / gateway 三条路拿到的是同一个答案。
    """
    # C6 consolidation: the **second** read path for `tool_refs` (plan_admission → derive A-axis capabilities here),
    # alongside subagent profile resolution. Unnormalized old names make the planner think the agent cannot query data,
    # while dispatch tests pass: silent capability loss. See docs/design/工具改名迁移.md.
    names = {canonical_tool_name(str(t or "").strip()) for t in (tool_names or [])}
    from bowhead_core.agents.capability_roles import agent_capabilities

    capability_ids = agent_capabilities(names)
    caps: set[str] = set()
    from bowhead_core.kb_search_budget import KNOWLEDGE_READ_TOOLS

    if "cap_knowledge_search" in capability_ids or KNOWLEDGE_READ_TOOLS.intersection(names):
        caps.add(READS_UPLOADS)
    if "cap_web_research" in capability_ids:
        caps.add(SEARCHES_WEB)
    if "cap_data_query" in capability_ids:
        caps.add(QUERIES_BI)
    return frozenset(caps)


def capabilities_by_agent(
    registry: Any,
    workspace_id: str | None,
    names: Iterable[str],
) -> dict[str, frozenset[str]]:
    """给一组 agent 名批量取能力表。

    **best-effort，逐个隔离**：某个 manifest 取不到/形状异常时只丢它自己，
    不让整张能力表塌成空 —— 空表会让 `select_capable_agent` 一个都挑不出来，
    把「注册表抖了一下」错报成「这活没人能干」。
    """
    out: dict[str, frozenset[str]] = {}
    for name in names:
        try:
            manifest = registry.get(name, workspace_id=workspace_id)
            allow = getattr(getattr(manifest, "tools", None), "allow", None)
            out[name] = derive_capabilities(allow)
        except Exception:  # noqa: BLE001,S112 — One broken manifest must not break the whole table.
            continue
    return out


def render_capabilities(caps: frozenset[str] | None) -> str:
    """能力标签 → 进 planner prompt 的一小段中文，如 `读上传文档/查本空间BI`。

    空能力（老 manifest、或 allowlist 里一个可判定工具都没有）返回空串，
    caller 据此**整段省略**——写「能力: (无)」会诱导 planner 以为这个 agent 废了，
    而它可能只是能力不在 A 轴这三个标签里（交付物能力要等第 2 期的 `produces:`）。
    """
    if not caps:
        return ""
    return "/".join(CAPABILITY_LABELS_ZH[c] for c in CAPABILITIES if c in caps)


def produces_by_agent(
    registry: Any,
    workspace_id: str | None,
    names: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """给一组 agent 名批量取**交付物声明**（B 轴，`AgentManifest.produces`）。

    ⚠ 与 A 轴的能力标签**刻意分成两张表**，不要合并：A 轴回答"够不够得着数据"、
    B 轴回答"产不产得出那种文件"，两者的匹配语义不同（A 轴是
    `required <= caps` 的子集判定，B 轴是格式命中）。合成一张表会让
    `required_capabilities` 的子集判定把格式名也算进去，静默失效。
    """
    out: dict[str, tuple[str, ...]] = {}
    for name in names:
        try:
            manifest = registry.get(name, workspace_id=workspace_id)
            values = getattr(manifest, "produces", None) or ()
            out[name] = tuple(str(v) for v in values)
        except Exception:  # noqa: BLE001,S112 — Isolate each item as in capabilities_by_agent.
            continue
    return out


def render_produces(produces: Iterable[str] | None) -> str:
    """交付物声明 → 进 planner prompt 的一小段中文，如 `PPT`。

    空 → 空串，caller 整段省略。**空的含义是「不产可下载文件（只落画布）」**，
    不是「这个 agent 废了」——绝大多数分析 agent 本来就只写画布报告。
    """
    if not produces:
        return ""
    labels = {"pptx": "PPT", "docx": "Word", "xlsx": "Excel", "pdf": "PDF"}
    seen = list(dict.fromkeys(produces))
    return "/".join(labels.get(p, p) for p in seen)


def required_capabilities(*, needs_web: bool, needs_corpus: bool) -> frozenset[str]:
    """一个计划步声明的「我要什么」→ 需要的能力标签。

    ⚠ **needs_web 优先于 needs_corpus，这是刻意的**，与被它替换掉的
    `route_step_agent` 旧实现逐字一致（WP-7）：外部公开信息（行业新闻/官方统计）
    根本不在上传文档里，两者同真时按 corpus 走会把这类步硬路由去搜本地 KB。
    """
    if needs_web:
        return frozenset({SEARCHES_WEB})
    if needs_corpus:
        return frozenset({READS_UPLOADS})
    return frozenset()


def select_capable_agent(
    preferred: str,
    required: frozenset[str],
    capabilities: Mapping[str, frozenset[str]],
    *,
    prefer: Iterable[str] = (),
) -> str | None:
    """挑一个**确实具备**所需能力的 agent；挑不出来返回 `None`。

    规则（刻意保持可预测，不引入任何打分）：

    1. 没有能力要求 → 尊重上游的选择，原样返回；
    2. 上游选的那个已经具备全部所需能力 → **保留它**。
       这一条是本期的实质改进：旧实现无条件 `return "compute"`，
       团队自建的 `market-researcher` 哪怕挂着 `web_search` 也会被顶掉；
    3. 否则从具备能力者里挑 —— **`prefer` 里的（内置 agent）优先**，同组内按名字排序；
    4. 一个都没有 → `None`。**这才是真的做不了**，调用方须如实告知，
       而不是随便塞一个够不到数据的 agent 上去空跑。

    ⚠ 第 3 条的 tiebreak 只是「确定性」，不是「最合适」。真正的区分度要等第 2 期的
    `produces:`（交付物声明）——在那之前，多个候选同时具备同一数据来源能力时，
    谁被选中本质上是任意的，这一点必须写明白，别让后人以为这里有智能。

    ★ `prefer` 是 2026-08-11 独立 review 加的（P2）：R3 之后团队自建 agent 与内置
    并进同一张能力表，而纯字母序兜底可以被**静默劫持** —— 任一成员建一个
    `team_wide`、名字排序靠前（如 `aaa-research`）、`tool_refs` 含 `web_search` 的 agent，
    此后凡是 planner 选了个不具备该能力的 agent、需要兜底的步，都会落到它身上，
    而它的 system_prompt 由创建者控制。越权面为零（执行仍走 caller-scoped 解析与降权，
    且计划卡逐步展示 agent 名给审批人），但「确定性兜底」这个属性会丢。
    ⇒ 兜底先在内置里找，内置无人具备时才轮到自建。
    """
    if not required:
        return preferred
    if required <= capabilities.get(preferred, frozenset()):
        return preferred
    preferred_pool = [n for n in sorted(capabilities) if n in set(prefer)]
    rest = [n for n in sorted(capabilities) if n not in set(prefer)]
    for name in (*preferred_pool, *rest):
        if required <= capabilities[name]:
            return name
    return None

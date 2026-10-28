"""TaskAgentRunner adapter — bridge manifest → existing SubagentRunner (PR-#15).

orchestrate_v2 接受 `agent_runner_fn: AgentRunnerFn` callable. PR-#11 测试用 mock callable.
此 PR 提供 **真实 LLM-backed adapter**: 把 AgentManifest 翻译成 SubagentProfile, 注入
PROFILES dict, 调老 SubagentRunner.run, 把 SubagentResult 翻译回 AgentRunnerFn signature.

设计 (最小侵入):
- 不修改 subagent.py 现 prod hot path
- 不修改 SubagentRunner 接口
- 在 `agents/` 子包内做翻译, 走 PROFILES module-level dict 注册 (与 custom_subagent_store
  机制 parallel)
- `register_manifest_as_profile(manifest, prompt_loader)` 幂等, 多次调用同名 manifest 覆盖

使用方式:
    from bowhead_core.agents.agent_runner_adapter import (
        make_legacy_agent_runner_fn, register_all_system_manifests,
    )
    register_all_system_manifests(registry)  # 启动一次
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=my_subagent_runner,
        parent_turn_id="t_abc",
        parent_bus=bus,
        parent_ctx=tool_exec_ctx,
    )
    result = await orchestrate_v2(..., agent_runner_fn=runner_fn, ...)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.agents.clarification_tool import is_clarification_sentinel
from bowhead_core.planning_v2 import planning_v2_enabled
from bowhead_core.subagent import (
    PROFILES,
    SubagentProfile,
    _materialize_authoring_pseudo_tools,
    render_spawn_language_line,
    resolve_spawn_language,
)

from .registry import _SYSTEM_PROMPT_DIR, AgentRegistry
from .task_spec import (
    PlanStepAgentBindingError,
    PlanStepBailError,
    PlanStepEmptyOutputError,
    PlanStepNeedsUserError,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bowhead_core.agent_execution_binding import ExactAgentExecutionBinding
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolExecutionContext
    from bowhead_core.types import TurnEventBus

    from .manifest import AgentManifest
    from .task_runner import AgentRunnerFn
    from .task_spec import TaskSpec
    from .turn_context import RuntimeTurnContext


# Type alias: load manifest.system.file contents.
PromptLoaderFn = Callable[["AgentManifest"], str]


# ★2026-08-18: send acceptance criteria to executor. Previously criteria only went through persistence → user rendering → verifier;
# executing agent never saw them. 116 observed 15/44 Top-N criteria failures with the same note shape:
# named TOP1/TOP2/last, never 3rd. Judge correct, exam never given to student.
_CRITERIA_CAP = 6
_CRITERIA_CHARS = 160


def _criteria_block(criteria: Sequence[str]) -> str:
    """验收标准 → prompt 块。**必须带反编造闸**否则这就是个「讨好评分器」的激励。"""
    items = [str(c).strip()[:_CRITERIA_CHARS] for c in criteria if str(c).strip()]
    if not items:
        return ""
    lines = "\n".join(f"{i}. {c}" for i, c in enumerate(items[:_CRITERIA_CAP], 1))
    return (
        "## 本步验收标准(用户已批准)\n"
        f"{lines}\n\n"
        "核验官**只读你产出的文字部分**(执行摘要 + 关键事实/KPI), 看不到明细表。"
        "所以上面每一条要求的具体名称与数值, **必须出现在摘要或关键事实里** —— "
        "算出来了却只留在表里, 等同于没交。\n"
        "⚠ 但**数据不支持哪一条, 就如实说明哪一条**(缺什么字段/为什么取不到), "
        "**绝不允许为了凑满这张清单编造名称或数字**。如实说明不算失败, 编造是最严重的事故。"
    )


_CHAPTER_FORM_CHARS = 120


def _chapter_block(deliverable: str) -> str:
    """「你只写这一章」约束 + 本步的产出形态 → prompt 块（plan-report-assembly 批 3-b）。

    为什么需要它：批 0/1/2 把**装订**修好了（116 t_c9853b490565：8 条腿的产出排成
    1 份 8 章、腿级 eyebrow/meta/建议/局限性归了文档级池）。但装订层只排序与归并、
    **不改写腿写的字**（这是「不要写手」决议的落地判据）⇒ 措辞降不了级：腿仍以为
    自己在交一份独立报告，于是各自写「本报告…」、各自重铺一遍全局背景、各自在末尾
    再来一段全局结论。结构对了，读起来还是八份东西。

    ★章名由写手按本章实际内容起，`deliverable` 只当**产出形态**下发（2026-09-25 用户拍板）。
    planner 的约定里步级 `deliverable` 就是「期望产出形态 (insight/表/章节)」（`plan_authoring.SYSTEM_PROMPT`），
    此前这里却把它当章名，只靠一张形态词表猜哪些不是章名——116 上先后出现 `## insight`（t_40869341f322）、
    `## 表+图+insight`（1.3.47 `s_6f8297c12cd1`）、`## 章节(表+图+关键指标)`（1.3.51 `s_bb8f798ebe34`），
    planner 换个写法词表就漏；同名的章在续轮修订时也点不中。

    ⚠ 决议 #4：**不给位次**。腿看不到前文（上游只喂 payload 摘要，不喂
    别章正文），给「第 3 章 / 共 8 章」会诱导它写「如前文所述」去指一段它根本没读过
    的内容 —— 那比重复背景更糟，是凭空的交叉引用。

    ❗**payload 字段与正文散文必须分开说**（批 3 自审逮到的 P1）：这一章的正文里不写
    「## 建议」「## 数据局限性」，但 payload 的那两个字段该填还填——它们不是同一件事。
    ⚠ 2026-09-07 起系统**不再**把 payload 的建议渲成画布上的一节（交回作者写进正文），
    所以这里不能再承诺「系统会在全文末尾统一渲一次」——那句话会让每条腿都不写建议、
    最终整份报告一条都没有。
    「正文里别再单起 `## 建议` 小节」，**绝不能说「建议不归你」** —— 腿要是照办
    不填这两个字段，文档级池子就空了，建议会从「每腿一组」变成**零组**，
    比原 bug 更糟。这与 2-b0 挡的是同一个净丢失。
    """
    form = str(deliverable or "").strip()[:_CHAPTER_FORM_CHARS]
    if not form:
        return ""
    return (
        f"## 本步交付的是整份文档里的一章\n本步的**产出形态**是「{form}」（这是形态要求，**不是**章标题）。\n\n"
        "这一章会和其它专家步的产出**装订成同一份文档**，封面、导语和全局收口由系统"
        "统一出。所以：\n"
        "- **只写这一章的内容**，写到这一章讲完为止。\n"
        "- **不写全文导语**、不写「本报告将…」这类对整份文档的自指。\n"
        "- **不写全局结论**、不做跨章总结 —— 收口是另一步的活。\n"
        "- 不重铺别章已经交代过的背景；本章需要的前提，一句话带过即可。\n"
        "- 章标题由你**按本章实际内容**起一个（点出本章的核心发现或主题，用本章自己的数据和对象说话），"
        "别把上面的产出形态当标题；`title` 就写这个章标题，别写成整份报告的大标题。\n"
        "- 若你**亲笔写正文**（调 `write_report`）：正文第一行写 `## <本章标题>`。"
        "亲笔轮的 payload **不会**再另渲一个标题出来，你不写这一行，这一章在文档里就是"
        "**无标题**的，读者会把它当成上一章的续写。（整篇报告骨架里「开头点题不加小标题」"
        "那条只适用于独立成篇，写章时按这里来。）\n"
        "- ⚠ payload 的 `recommendations` 与 `caveats` 有就填：它们是给 Lead 写对话回复用的"
        "结构化证据，**不会**被渲成画布上的一节。"
        "这一章的正文里也不写「## 建议」「## 数据局限性」——那是整份文档的收尾，不是你这一章的。"
    )


def _slim_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """结构感知瘦身(planning-mode-audit WP-3): 保决策承重键(title/exec_summary/
    insights[].thesis/recommendations/error), 砍长 narrative/evidence 尾巴。"""
    slim: dict[str, Any] = {"_truncated": True}
    for k in ("title", "eyebrow", "exec_summary"):
        if payload.get(k):
            slim[k] = payload[k]
    ins = payload.get("insights")
    if isinstance(ins, list) and ins:
        slim["insights"] = [{kk: i.get(kk) for kk in ("title", "thesis") if i.get(kk)} for i in ins[:8] if isinstance(i, dict)]
    recs = payload.get("recommendations")
    if isinstance(recs, list) and recs:
        slim["recommendations"] = recs[:8]
    if payload.get("error"):
        slim["error"] = payload["error"]
    return slim


def render_upstream_block(
    upstream: Any,
    *,
    item_chars: int | None = None,
    total_chars: int | None = None,
) -> str:
    """upstream payloads → prompt 块, 逐条预算(planning-mode-audit WP-3)。

    替换旧 `json.dumps(list)[:4000]` 字符级盲切 —— 那会让第一个大 payload 挤掉
    后面所有条目(07-16 prod 案: writer 只见 1/2 个 kb payload 还被要求"汇总四份
    分析"), 且下游对截断零感知。规则: 每条独立 dumps; 单条超 per-item 预算 →
    结构感知瘦身 + `_truncated` 标记; 总预算超限 → 丢整条并在块尾写明省略了
    哪些 task_id —— **writer 对截断永远有感知**。
    """
    import os as _os

    item_cap = item_chars or int(_os.environ.get("BOWHEAD_PLANNING_UPSTREAM_ITEM_CHARS", "6000") or 6000)
    total_cap = total_chars or int(_os.environ.get("BOWHEAD_PLANNING_UPSTREAM_TOTAL_CHARS", "24000") or 24000)
    parts: list[str] = []
    used = 0
    omitted: list[str] = []
    for item in upstream or ():
        tid = str(item.get("task_id", "?")) if isinstance(item, dict) else "?"
        s = json.dumps(item, ensure_ascii=False, indent=2, default=str)
        if len(s) > item_cap and isinstance(item, dict) and isinstance(item.get("payload"), dict):
            s = json.dumps({**item, "payload": _slim_payload(item["payload"])}, ensure_ascii=False, indent=2, default=str)
        if len(s) > item_cap:
            s = s[:item_cap] + '\n…("_truncated": true — 该条超预算已截断)'
        if used + len(s) > total_cap:
            omitted.append(tid)
            continue
        parts.append(s)
        used += len(s)
    out = "## 上游 task 已产出 (typed payload):\n" + "\n".join(parts)
    if omitted:
        out += f"\n⚠ 超出总预算已省略 {len(omitted)} 条上游产出(task_id: {', '.join(omitted)}) — 汇总时须注明这些部分缺失。"
    return out


_WEB_SOURCE_TOOLS = ("web_search", "web_fetch", "web_extract", "web_crawl", "web_deep_research")
_MAX_BACKFILL_SOURCES = 10


def _sources_from_web_invocations(invocations: list[Any]) -> list[dict[str, Any]]:
    """web_search/web_fetch invocation 结果 → sources[] 条目(确定性回填, S6 F-D2).

    web_search result 形状 = {"query", "summary", "results": [{"title","url","snippet"}]}
    (web_search.py 契约)；web_extract / web_crawl 是 ``pages[{url,…}]``，web_deep_research 是
    ``sources[{title,url,…}]``。按 url(缺则 title)去重, cap 10。纯函数可测。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for inv in invocations:
        if getattr(inv, "tool_name", "") not in _WEB_SOURCE_TOOLS:
            continue
        res = getattr(inv, "result", None)
        if not isinstance(res, dict):
            continue
        args = getattr(inv, "arguments", None) or {}
        query = str(args.get("query") or args.get("input") or "")
        for r in [*(res.get("results") or []), *(res.get("pages") or []), *(res.get("sources") or [])]:
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or "").strip()
            label = str(r.get("title") or "").strip() or url
            key = url or label
            if not key or key in seen:
                continue
            seen.add(key)
            entry: dict[str, Any] = {"label": label[:120], "tool": str(getattr(inv, "tool_name", "web_search"))}
            if query:
                entry["query"] = query[:120]
            if url:
                entry["url"] = url
            out.append(entry)
            if len(out) >= _MAX_BACKFILL_SOURCES:
                return out
    return out


def default_prompt_loader(manifest: AgentManifest) -> str:
    """从 system 目录读 manifest.system.file 内容.

    PR-#2 user manifest 走 user/<workspace_id>/ 路径, 此 default loader 仅 system.
    User-aware loader 调用方自己注入 (workspace_id 上下文).

    Sprint θ Phase 8b-4: returns empty string when manifest.system is None
    (Lead orchestrator: builds prompt in-memory per turn via prompts.lead).
    """
    if manifest.system is None:
        return ""
    file = manifest.system.file
    path = _SYSTEM_PROMPT_DIR / file
    if not path.is_file():
        raise FileNotFoundError(f"prompt file not found: {path}")
    body = path.read_text(encoding="utf-8")
    if manifest.name == "compute":
        from .prompts.simple_lookup_locale import bind_lookup_template_source

        body = bind_lookup_template_source(body)
    return body


# dev 1.0.77 dogfood P2-d: Chinese question → English report title / KPI labels / body, with only backend
# deterministic meta line Chinese. Repo previously had **no** language constraint, not an overridden one;
# none was written, so model followed evidence (naturally English BI column names) into English.
#
# Put in `manifest_to_profile`: sole system-prompt convergence for all peers (system manifest / user custom / loaders).
# Adding the same rule to 8 `prompts/system/*.md` files duplicates it 8 times, inevitably missing peer 9
# (cross-cutting duplication in 4 places already signals architectural error).
_LANGUAGE_RULE = (
    "**语言跟随用户**：你产出的所有用户可见文字（报告标题、执行摘要、洞察标题与正文、"
    "KPI 标签、图表标题与轴名、行动建议、以及最终回答）一律使用**用户提问时所用的语言**"
    "——用户用中文问就全部用中文，用英文问就全部用英文。数据源的列名/字段名是英文"
    "**不构成**改用英文的理由（BI 列名天然是英文）：把它们译成用户的语言，专有名词"
    "（品牌名、公司名、技术术语）可保留原文。"
)


def manifest_to_profile(
    manifest: AgentManifest,
    prompt_loader: PromptLoaderFn = default_prompt_loader,
) -> SubagentProfile:
    """AgentManifest → SubagentProfile 翻译.

    Mapping:
    - name → name
    - tools.allow tuple → tool_whitelist
    - max_iterations / max_output_tokens → 同名
    - model alias → llm_agent_type ("analyst" / "modeler" / "narrative" 等)
    - system.file 内容 + system.append → system_prompt
    """
    body = prompt_loader(manifest)
    if manifest.name == "compute":
        from .prompts.simple_lookup_locale import bind_lookup_template_source

        body = bind_lookup_template_source(body)
    # Phase 8b-4: manifest.system can be None (Lead bypass). default_prompt_loader
    # returns "" in that case. Skip append handling when system is None.
    if manifest.system is not None and manifest.system.append:
        body = body + "\n\n" + manifest.system.append
    # Multilingual batch 2: this "follow question language" rule **opposes** prompt-tail
    # `build_language_directive` "answer in settings language", which is later and more specific.
    # Both live policies are unfinished work from 2026-08-11 introduction (then sole repo language constraint);
    # no one revisited after 08-17 deterministic propagation.
    # Worse, **uneven coverage**: this only handles manifest profiles; team/user custom
    # subagents manually construct `SubagentProfile` in `_resolve_profile`, bypassing it.
    # ⇒ With flag on, no append here; rule becomes `_legacy_run` fallback **without language_context**,
    # effective for **all** executors there, including custom subagents and Lead.
    from bowhead_core.effective_language import language_decision_v2_enabled

    if body and not language_decision_v2_enabled():
        body = body + "\n\n" + _LANGUAGE_RULE
    from bowhead_core.system_plugins import resolve_system_plugins

    resolved_tools, resolved_grants = resolve_system_plugins(manifest.plugins, manifest.tools.allow, manifest.skill_grants)
    tool_whitelist, runtime_prelude, skill_invoke_allowlist = _materialize_authoring_pseudo_tools(tuple(resolved_tools))
    primary_capability_ref = None
    manifest_primary_capability = getattr(manifest, "primary_capability", None)
    if manifest_primary_capability is not None:
        from bowhead_core.agent_capability_catalog import latest_active_capability_ref

        primary_capability_ref = latest_active_capability_ref(manifest_primary_capability)
    from bowhead_skill.runtime_scope import ExactSkillRuntimeScope

    from bowhead_core.system_skill_snapshot import parse_system_manifest_skill_grants

    # Re-read even a server-side model_copy; no name/tool discovery creates grants.
    raw_grants = resolved_grants
    grants = None if raw_grants is None else parse_system_manifest_skill_grants(raw_grants)
    scope = (
        None
        if grants is None
        else ExactSkillRuntimeScope(
            read_names=tuple(sorted(grant.name for grant in grants if "read" in grant.permissions)),
            invoke_names=tuple(sorted(grant.name for grant in grants if "invoke" in grant.permissions)),
        )
    )
    return SubagentProfile(
        name=manifest.name,
        llm_agent_type=manifest.model,
        tool_whitelist=tool_whitelist,
        system_prompt=runtime_prelude + body,
        max_iterations=manifest.max_iterations,
        max_output_tokens=manifest.max_output_tokens,
        description=manifest.description,
        # agent-capability-routing 2a: earlier comment ("AgentManifest extra='forbid',
        # **no deck_inputs field**, therefore name-derived") stated why: originally wanted manifest
        # declaration, falling back to names only because field absent. Now `produces` exists; use declaration.
        # Discovery §2.1 missed this **4th** name-hardcoding site; three of four now removed
        # (①/③ prompt prose is phase 3; do not advance it).
        # Behavior unchanged: today `produces: [pptx]` matches only compute / consultant,
        # byte-identical old name set; removing names does not change behavior (test_deck_inputs_*).
        deck_inputs=("pptx" in manifest.produces),
        # tools sprint WP2c: the Lead orchestrates, it does NOT execute team tools directly — give it an
        # EMPTY team_tool_bundle so the per-turn effective-whitelist (`static ∪ (overlay ∩ bundle)`)
        # excludes every teammcp.* by the WHITELIST filter, NOT the role-ACL stamp. This makes team-tool
        # binding bundle-driven (the correct axis) instead of relying on the analyst role stamp as the
        # Lead-exclusion crutch (spike-role-binding.md). The admitted settings helper also
        # excludes Team overlays; other built-in peers retain the full overlay through None.
        team_tool_bundle=() if manifest.name in {"lead", "configuration-agent", "agent-coach"} else None,
        skill_invoke_allowlist=skill_invoke_allowlist,
        skill_runtime_scope=scope,
        skill_runtime_grants=grants,
        primary_capability_ref=primary_capability_ref,
        legacy_image_deliverable_authority=(primary_capability_ref == "cap_visualize@1" and "image_generate" in tool_whitelist),
        # Declared in the manifest, never derived from the routing name.
        capability_protocol_block=bool(getattr(manifest, "capability_protocol_block", False)),
    )


def register_manifest_as_profile(
    manifest: AgentManifest,
    prompt_loader: PromptLoaderFn = default_prompt_loader,
) -> None:
    """注入 PROFILES dict (幂等, 同名覆盖). SubagentRunner._resolve_profile 自动看到."""
    PROFILES[manifest.name] = manifest_to_profile(manifest, prompt_loader)


def register_all_system_manifests(registry: AgentRegistry) -> int:
    """启动时注册所有 system manifest 到 PROFILES. 返回 count.

    User manifest per-workspace 注册由 turn_runner 在 turn 起始调
    `register_manifest_as_profile` (workspace-aware loader).
    """
    count = 0
    for name in registry.list_all():
        manifest = registry.get(name)
        try:
            register_manifest_as_profile(manifest)
            count += 1
        except FileNotFoundError:
            # PR-#7: skip missing canvas-writer/etc. prompt files in test environments.
            continue
    return count


_BAIL_MESSAGE_KEYS = ("message", "reason", "detail", "error")
_BAIL_MESSAGE_CAP = 300


def _bail_message(envelope: object) -> str:
    """从 bail 信封里取**给人看的**那句话。

    ★2026-08-14 116 dogfood：此前 bail 只带 kind，用户在计划卡上看到的字面是
    「Failed: transient: transient」（`TaskFailureError.__str__` 把 kind 拼了两遍），
    而真因（BI 查询下载超时 60s）在 UI 上根本不存在。信封契约**本来就要求**这句话
    （`analysis_payload` 校验：`error.message missing/empty (need 1-2 sentence reason)`）。

    多个键名是因为两条 bail 路的信封形状不同（结构化 error 信封 vs 工具返回的
    `bail_kind` dict），取第一个非空的即可；截断防止把整段 traceback 灌进卡片。
    """
    if not isinstance(envelope, dict):
        return ""
    for key in _BAIL_MESSAGE_KEYS:
        val = envelope.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:_BAIL_MESSAGE_CAP]
    return ""


@dataclass(frozen=True)
class PlanAgentRunner:
    """Keep batch admission beside the existing per-step execution adapter."""

    run: AgentRunnerFn
    prepare_batch: Callable[[Sequence[TaskSpec]], Awaitable[None]]

    async def __call__(self, manifest: AgentManifest, spec: TaskSpec, runtime_ctx: RuntimeTurnContext) -> tuple[dict[str, Any], int, int]:
        return await self.run(manifest, spec, runtime_ctx)


def make_legacy_agent_runner_fn(
    *,
    subagent_runner: SubagentRunner,
    parent_turn_id: str,
    parent_bus: TurnEventBus,
    parent_ctx: ToolExecutionContext,
    # ★ Required, no default: security guard. Default empty set makes it opt-in;
    # a future second call site omitting it would **silently disable** protection, with no failing test.
    team_sourced_names: frozenset[str],
) -> PlanAgentRunner:
    """构造 AgentRunnerFn, 调老 SubagentRunner.run 跑.

    Returns the closure that orchestrate_v2 will call as `agent_runner_fn`.

    Args:
        subagent_runner: 现有 SubagentRunner instance (含 router + tool_registry).
        parent_turn_id / parent_bus / parent_ctx: turn-level refs (turn_runner 注入).

    Note: parent_* 是同一 turn 内所有 task 共享的 (并行 dispatch 时共用).
        Per-task 隔离由 SubagentRunner 内部 subagent_id 保证.
    """

    dispatch_attempts: dict[str, int] = {}

    from bowhead_core.agent_collaboration_runtime import CollaborationRuntime
    from bowhead_core.agent_execution_binding import (
        AgentExecutionBindingError,
        AgentExecutionBindingStore,
        ExactAgentBindingExpectation,
    )

    collaboration = getattr(parent_ctx, "collaboration_runtime", None)
    reference_plan = getattr(parent_ctx, "_reference_plan_runtime", None)
    participates = isinstance(collaboration, CollaborationRuntime) and reference_plan is None
    prepared: dict[str, ExactAgentExecutionBinding | Exception | None] = {}

    async def _admit(spec: TaskSpec, ctx: ToolExecutionContext) -> ExactAgentExecutionBinding | None:
        """Freeze the current authorized step, then explicitly join collaboration."""
        choice = getattr(spec, "agent_choice", None)
        reference_main = reference_plan is not None and reference_plan.is_main_step(spec.task_id)
        if not (reference_main or choice is not None or participates):
            return None
        try:
            if reference_main:
                binding = await reference_plan.admit_step(runner=subagent_runner, spec=spec, ctx=ctx)
                if binding is None or binding.runtime_profile_name != spec.agent_name:
                    raise PlanStepAgentBindingError("reference_plan_receiver_changed")
            else:
                expected = None
                if choice is not None:
                    from bowhead_core.agent_behavior_flags import agent_behavior_m3_enabled

                    if not agent_behavior_m3_enabled():
                        raise PlanStepAgentBindingError("agent_behavior_reader_disabled")
                    if choice.runtime_profile_name != spec.agent_name:
                        raise PlanStepAgentBindingError("agent_candidate_stale")
                    expected = ExactAgentBindingExpectation(
                        agent_id=choice.agent_id,
                        assignment_id=choice.assignment_id,
                        revision_ref=choice.revision_ref,
                        release_id=choice.release_id,
                        behavior_fingerprint=choice.behavior_fingerprint,
                    )
                attempt = dispatch_attempts.get(spec.task_id, 0) + 1
                dispatch_attempts[spec.task_id] = attempt
                binding = await subagent_runner.admit_execution_binding(
                    profile_name=spec.agent_name,
                    parent_turn_id=parent_turn_id,
                    parent_ctx=ctx,
                    dispatch_key=f"step:{spec.task_id}:attempt:{attempt}",
                    agent_id=choice.agent_id if choice is not None else None,
                    expected=expected,
                    dispatch_source="plan",
                )
                if binding is None and choice is not None:
                    raise PlanStepAgentBindingError("agent_admission_unavailable")
            # Legacy personal profiles have no exact revision. They keep the
            # original nonparticipant path instead of borrowing Lead's identity.
            if binding is not None and participates:
                collaboration.require_context(ctx)
                await collaboration.store.join(
                    collaboration.worker,
                    execution_id=binding.execution_id,
                    assignment=spec.user_question.encode()[:4096].decode(errors="ignore"),
                    expected_output=(getattr(spec, "deliverable", "") or "Complete the accepted step with evidence and limitations.")
                    .encode()[:4096]
                    .decode(errors="ignore"),
                    assistance_boundary="Answer bounded questions about this Plan step. Ask Lead about scope or unavailable peers; retain your assignment.",
                )
            return binding
        except AgentExecutionBindingError as exc:
            raise PlanStepAgentBindingError(exc.code) from exc

    async def _prepare_batch(specs: Sequence[TaskSpec]) -> None:
        if not participates:
            return
        # Only the driver's gated runnable frontier is admitted. Every accepted
        # sibling is discoverable before the first model call. Per-step refusals
        # still use LocalTaskRunner's normal failed-step classification.
        for spec in specs:
            try:
                prepared[spec.task_id] = await _admit(spec, parent_ctx)
            except Exception as exc:
                prepared[spec.task_id] = exc
        await collaboration.notify()

    async def _runner_fn(
        manifest: AgentManifest,
        spec: TaskSpec,
        _runtime_ctx: RuntimeTurnContext,
    ) -> tuple[dict[str, Any], int, int]:
        # Phase 7.5-C P0 #5 fix: set _CURRENT_PEER_NAME_CONTEXTVAR for I3 runtime
        # guard. Without this, canvas_tools._check_i3_canvas_writer_only() blocks
        # every legitimate canvas-writer call (default ContextVar is "lead").
        from ._lead_context import (
            reset_current_peer_name,
            set_current_peer_name,
        )

        peer_token = set_current_peer_name(manifest.name)

        # Ensure manifest registered (idempotent)
        #
        # ★★ Team/custom agents **must never enter here** (agent-capability-routing R2).
        # `PROFILES` is **process-global**, checked by `_resolve_profile` **before** team branch:
        #   ① Registration bypasses all team security normalization (prompt-injection lint /
        #      filter_destructive / nonanalyst tool demotion / required subagent_return / model clamp);
        #   ② Worse, global means **everyone in this process** can resolve it,
        #      invalidating `creator_only` (all 6 observed 116 custom agents had this visibility).
        # Team agents need no registration: `subagent_runner.run(profile_name=...)` below
        # uses `resolve_for_caller` team branch to get an **already-normalized** profile.
        if manifest.name not in PROFILES and manifest.name not in team_sourced_names:
            register_manifest_as_profile(manifest)

        # Translate upstream_payloads → extra_instructions (old path lacks typed payloads).
        extra = ""
        if spec.upstream_payloads:
            if planning_v2_enabled():
                # WP-3: per-item budget + disclosed truncation (render_upstream_block docstring).
                extra = render_upstream_block(spec.upstream_payloads)
            else:
                extra = "## 上游 task 已产出 (typed payload):\n"
                extra += json.dumps(
                    list(spec.upstream_payloads),
                    ensure_ascii=False,
                    indent=2,
                )[:4000]  # Cap prompt growth.
        if spec.routing_reason:
            extra = (f"## 本次路由原因\n{spec.routing_reason}\n\n" + extra).rstrip()

        # ──── Sprint θ Phase 8b-2: δ wire #2 (G3 wire #2) ──────────────────
        # Per tech-design §0.6 F1 + A1 + Phase 7.5-A CTO P0 #1 fix: read fact_store
        # from ContextVar set by lead_orchestrator.run_turn head. AWAIT async
        # list_active (was bug — silent coroutine → TypeError swallowed).
        # Narrow except (was P0 #2): only catch storage/lookup errors, let bugs
        # like TypeError propagate so tests catch them.
        from bowhead_core.agents._lead_context import get_lead_fact_store
        from bowhead_core.agents.prompts import active_facts as _active_facts

        fact_store = get_lead_fact_store()
        if fact_store is not None:
            workspace_id = _runtime_ctx.serializable.workspace_id
            # Phase 8b-4 will add project_id field; until then graceful fallback
            project_id = getattr(_runtime_ctx.serializable, "project_id", None)
            # δ scope-leak fix (mirror lead_orchestrator): pass user_id so the peer
            # can read back user-scoped facts; without it _scope_filter drops them.
            user_id = getattr(_runtime_ctx.serializable, "user_id", None) or None
            try:
                # CTO P0 #1: await async list_active (was missing)
                facts = await fact_store.list_active(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    user_id=user_id,
                )
            except (LookupError, OSError, RuntimeError) as exc:
                # Narrow per CTO P0 #2: swallow only storage layer issues
                import logging

                logging.getLogger(__name__).warning(
                    "delta_wire_2_list_active_failed manifest=%s err=%s — peer continues without δ",
                    manifest.name,
                    exc,
                )
                facts = []
            if facts:
                delta_block = _active_facts.build_block(facts, workspace_id=workspace_id)
                if delta_block:
                    # Prepend: δ active_facts at top so peer LLM sees first
                    parts = [delta_block]
                    if extra:
                        parts.append(extra)
                    extra = "\n\n".join(parts)
        # ──── End δ wire #2 ────────────────────────────────────────────────

        # Phase 7.5-C P1 #3 fix: per-peer ToolExecutionContext rebuild — override
        # workspace_id + kb_role from dispatcher-propagated sctx (I4 invariant).
        # Without this, peer KB tools read parent_ctx.kb_role (turn-level) ignoring
        # any cross-tenant override; cross-workspace fact leak risk.
        from dataclasses import replace as _dataclass_replace

        reference_plan = getattr(parent_ctx, "_reference_plan_runtime", None)
        peer_ctx = _dataclass_replace(parent_ctx) if reference_plan is not None else parent_ctx
        sctx_kb_role = getattr(_runtime_ctx.serializable, "kb_role", None)
        sctx_workspace_id = _runtime_ctx.serializable.workspace_id
        try:
            ctx_overrides: dict[str, object] = {}
            if peer_ctx.workspace_id != sctx_workspace_id:
                ctx_overrides["workspace_id"] = sctx_workspace_id
            if sctx_kb_role and peer_ctx.kb_role != sctx_kb_role:
                ctx_overrides["kb_role"] = sctx_kb_role
            if ctx_overrides:
                peer_ctx = _dataclass_replace(peer_ctx, **ctx_overrides)
        except (TypeError, AttributeError):
            # If replace fails (e.g. parent_ctx is mock), fall through with parent_ctx
            pass

        # spawn-language: plan execution bypasses subagent_spawn handler (lesson: injection on one path
        # left the other unprotected); reuse same implementation for deterministic ctx
        # fallback. Plan step has no Lead spawn argument → param=None, output_language only.
        _lang_line = render_spawn_language_line(resolve_spawn_language(None, peer_ctx))
        if _lang_line:
            extra = (_lang_line + "\n\n" + extra).rstrip() if extra else _lang_line

        # Batch 3-b: chapter name and "write only this chapter" constraint adjacent to criteria; both are **output contracts**
        # read beside task. Chapter before criteria: first know output shape,
        # then scoring. Missing planner deliverable returns empty string ⇒ byte-identical behavior.
        _deliverable = str(getattr(spec, "deliverable", "") or "")
        if getattr(parent_bus, "_d6_plan_deliverable_kind", None) == "answer":
            _chap_block = (
                "## Plan delivery\nThis Plan delivers a chat answer. Return your findings through subagent_return; "
                "do not use write_report or create report chapters. Separately requested charts and files keep their existing Tools."
            )
            if _deliverable.strip():
                _chap_block += f"\nRequested step output shape: {_deliverable.strip()[:_CHAPTER_FORM_CHARS]}"
        else:
            _chap_block = _chapter_block(_deliverable)
        if _chap_block:
            extra = (extra + "\n\n" + _chap_block).strip() if extra else _chap_block

        # Acceptance criteria last: output contract read adjacent to task.
        _crit_block = _criteria_block(getattr(spec, "success_criteria", ()) or ())
        if _crit_block:
            extra = (extra + "\n\n" + _crit_block).strip() if extra else _crit_block

        # Call legacy SubagentRunner.run.
        exact_binding = None
        binding_lifecycle_store = None

        async def _mark_exact_terminal(
            status: Literal["completed", "failed", "blocked", "cancelled"],
            failure_code: str | None = None,
        ) -> None:
            if binding_lifecycle_store is None or exact_binding is None:
                return
            try:
                await binding_lifecycle_store.mark_terminal(
                    exact_binding.execution_id,
                    status=status,
                    failure_code=failure_code,
                )
                if participates:
                    await collaboration.notify()
            except Exception:
                logger.exception(
                    "plan.execution_terminal_persist_failed execution=%s",
                    exact_binding.execution_id,
                )

        reference_main = reference_plan is not None and reference_plan.is_main_step(spec.task_id)
        try:
            exact_binding = prepared.pop(spec.task_id) if spec.task_id in prepared else await _admit(spec, peer_ctx)
            if isinstance(exact_binding, Exception):
                raise exact_binding
            if exact_binding is not None:
                binding_lifecycle_store = AgentExecutionBindingStore(str(subagent_runner.team_subagent_store.db_path))
                await binding_lifecycle_store.mark_running(exact_binding.execution_id)
            if isinstance(collaboration, CollaborationRuntime) and not participates:
                from copy import copy

                peer_ctx = copy(peer_ctx)
                peer_ctx.collaboration_runtime = None
                peer_ctx.collaboration_execution = None
                peer_ctx.collaboration_call = None
        except BaseException:
            reset_current_peer_name(peer_token)
            raise
        if "render_deck" in manifest.tools.allow:
            # Same new-deck preparation as subagent_spawn for an Agent granted the native renderer
            # (the Tool still admits only its server-stamped deck peer). The binding lives on this
            # step's own context copy; parallel steps share parent_ctx and must never see it.
            from copy import copy

            from bowhead_core.deck_render_inputs import prepare_deck_render_inputs

            peer_ctx = copy(peer_ctx)
            peer_ctx.deck_render_binding = None
            try:
                deck_block = await prepare_deck_render_inputs(
                    canvas_store=getattr(peer_ctx, "canvas_store", None),
                    artifact_store=getattr(peer_ctx, "artifact_store", None),
                    session_id=getattr(peer_ctx, "session_id", "") or "",
                    user_id=getattr(peer_ctx, "user_id", None),
                    mode="consultant",
                    context=peer_ctx,
                )
            except Exception:
                logger.warning("plan.consultant_deck_inputs_failed task=%s", spec.task_id, exc_info=True)
                deck_block = None
            if deck_block:
                extra = deck_block + ("\n\n" + extra if extra else "")
        try:
            # graph-mode batch 4 D-A (derive): plan-step subagent_id derives from step ID.
            # (spec.task_id=step id, plan_reify.step_to_task)——tool_result/
            # agent_spawned naturally carries step ownership, zero-join provenance export; calls emitted
            # before failed-step interruption retain attribution. Derive only for plan steps (ps_ prefix);
            # ordinary multi-agent task_id auto-defaults t_*, unchanged ID shape.
            _tid = str(getattr(spec, "task_id", "") or "")
            _pre_minted = f"sub_{_tid}" if _tid.startswith("ps_") else None
            run_kwargs: dict[str, Any] = {
                "profile_name": manifest.name,
                "task": spec.user_question,
                "parent_turn_id": parent_turn_id,
                "parent_bus": parent_bus,
                "parent_ctx": peer_ctx,
                "extra_instructions": extra or None,
                "pre_minted_subagent_id": _pre_minted,
            }
            if exact_binding is not None:
                run_kwargs["exact_binding"] = exact_binding
            if reference_main:
                result = await reference_plan.run_step(runner=subagent_runner, spec=spec, binding=exact_binding, ctx=peer_ctx, run_kwargs=run_kwargs)
            else:
                result = await subagent_runner.run(**run_kwargs)
        except BaseException as exc:
            if exact_binding is not None:
                from bowhead_core.agent_execution_binding import AgentExecutionBindingError

                if isinstance(exc, AgentExecutionBindingError):
                    await _mark_exact_terminal("blocked", exc.code)
                    raise PlanStepAgentBindingError(exc.code) from exc
            if isinstance(exc, asyncio.CancelledError):
                await _mark_exact_terminal("cancelled")
            else:
                await _mark_exact_terminal("failed", "agent_peer_exception")
            raise
        finally:
            # Reset peer ContextVar regardless of outcome (P0 #5 cleanup)
            reset_current_peer_name(peer_token)

        if not result.ok:
            # SubagentRunner failure → raise for LocalTaskRunner wrapping as TaskFailureError.
            err_msg = result.error or "unknown subagent failure"
            await _mark_exact_terminal("failed", "agent_peer_failed")
            raise RuntimeError(err_msg)

        # SubagentResult.payload → dict expected by orchestrate_v2.
        payload = result.payload or {}
        if not isinstance(payload, dict):
            payload = {"raw": str(payload)}

        # ── planning-mode-audit P1-a/P1-b: three checks for ok=True but invalid output (order matters:
        # sentinel peer is ok=True, often both empty; same result may match all checks, so most specific first).
        # Plan execution only: this adapter's sole caller is plan driver;
        # ordinary chat uses subagent_spawn wrapper with its own empty-answer guard.
        if planning_v2_enabled():
            _invs = result.tool_invocations or []
            # ① Clarification sentinel (e.g. Datarc workspace picker): child loop sees
            #    sentinel, hard-breaks, returns ok=True/empty output; previously counted DONE
            #    (discovery P0-1). Upgrade to step BLOCKED with clarification passthrough.
            for _inv in _invs:
                _ir = getattr(_inv, "result", None)
                if is_clarification_sentinel(_ir):
                    _clar = _ir.get("clarification") if isinstance(_ir, dict) else None
                    await _mark_exact_terminal("blocked", "needs_user_input")
                    raise PlanStepNeedsUserError(
                        detail_json=json.dumps(_clar or {}, ensure_ascii=False, default=str),
                    )
            # ②-b π-B hard bail: subagent_return result dict contains `bail_kind`.
            #    ⚠ Criterion only bail_kind: every successful return has `__terminal` too
            #    (subagent_return_tool success sets TERMINAL_FLAG); both keys live only
            #    in tool-result dict, while stashed payload only has narrative/fallback_markdown
            #    (WP-1 review P1-1a: reading payload was a dead branch).
            for _inv in _invs:
                _ir = getattr(_inv, "result", None)
                _bk = _ir.get("bail_kind") if isinstance(_ir, dict) else None
                if _bk:
                    # Human explanation follows kind; kind alone yields "Failed: transient: transient".
                    await _mark_exact_terminal("failed", str(_bk))
                    raise PlanStepBailError(str(_bk), message=_bail_message(_ir))
            # ②-a Structured error envelope (analysis_payload contract, in stashed payload):
            #    nonempty payload declaring failure cannot count as success (Datarc outage shape,
            #    review P0). needs_user_input follows NeedsUser, with
            #    error.clarification_payload (WP-1 review P2-1b: otherwise BLOCKED
            #    hint points to empty content).
            _env_err = payload.get("error")
            if isinstance(_env_err, dict) and _env_err.get("kind"):
                _kind = str(_env_err["kind"])
                if _kind == "needs_user_input":
                    await _mark_exact_terminal("blocked", "needs_user_input")
                    raise PlanStepNeedsUserError(
                        detail_json=json.dumps(
                            _env_err.get("clarification_payload") or {},
                            ensure_ascii=False,
                            default=str,
                        ),
                    )
                await _mark_exact_terminal("failed", _kind)
                raise PlanStepBailError(_kind, message=_bail_message(_env_err))
            # ③ Both empty: no payload or prose; step produced nothing.
            if not payload and not (result.final_answer or "").strip():
                await _mark_exact_terminal("failed", "empty_output")
                raise PlanStepEmptyOutputError()

        # Without payload but with final_answer Markdown, wrap SubagentResult as dict.
        if not payload and result.final_answer:
            payload = {"fallback_markdown": result.final_answer[:8000]}

        # graph-mode S6 false-negative fix (dogfood F-D2): web-research peers routinely omit sources[]
        # (unreliable LLM emission; schema description added, backend fallback still required); deterministically
        # backfill actual web invocations so verifier source-labeling criteria and citation attribution
        # can see evidence. Fill only, never overwrite peer-supplied sources.
        if payload and not payload.get("sources"):
            _web_sources = _sources_from_web_invocations(result.tool_invocations or [])
            if _web_sources:
                payload["sources"] = _web_sources

        # planning-mode-audit WP-5: collect all plan-step tool invocations for this turn
        # (like _m137 bag, closure parent_ctx is exactly what normal compose reads).
        # Normal compose citation binding/BI-content fallback + fabrication-guard observed numbers
        # all consume it. Synchronous init-if-absent without await (single-loop gather has no race, architecture-#6).
        if planning_v2_enabled():
            _plan_bag = getattr(parent_ctx, "_plan_step_invocations", None)
            if _plan_bag is None:
                _plan_bag = []
                try:
                    parent_ctx._plan_step_invocations = _plan_bag
                except (AttributeError, TypeError):
                    _plan_bag = None
            if _plan_bag is not None:
                _plan_bag.extend(result.tool_invocations or [])

        await _mark_exact_terminal("completed")

        return payload, result.tokens_input, result.tokens_output

    return PlanAgentRunner(_runner_fn, _prepare_batch)

"""Canvas 实质性标记 —— 「这一轮到底有没有产出结论」，在 **compose 期**判一次并落库。

## 为什么在 compose 期判，而不是事后看画布

评估见 `docs/sprints/empty-report-library-2026-08-25/assessment.md`。一句话：
**事后从画布形状反推做不到**，我在 116 全库上实测出双向错判——

- **误杀**：`cv_c4c9b12c7586`「查一下最近的销售表现」是完整报告（9 处引注、
  营业额 -5.2% / 利润 -18.7% / 客流 +3.8%、正文内嵌 markdown 表格、`[figure:]`、`## 建议`），
  却**一个 `chart`/`data_table`/`recommendations` 段都没有**——散文写手把这些全写进 narrative
  的 markdown 里了。按 section 种类判会把它藏掉。而这一类**正在变多**（plan-report-assembly
  批 3 把子 agent 推向亲笔散文路，116 实测 7/8 翻路）。
- **漏杀**：`cv_53ea95cfb793` 标题逐字「2026年Q1综合经营分析报告 — 数据不足，无法编制」，
  却带着 `kpi_row` + 3 张 `insight_card` + `recommendations`——agent 会围绕「没有数据」
  这件事本身产出结构化内容。

也不能复用管线既有的失败信号：那 42 份里 **37 份是 `status=completed` + `error_kind=(none)`**，
在管线眼里就是成功轮（BI 正常返回，只是零行）。「没有结论」是内容判断，不是错误状态。

compose 期则还握着 payload、工具结果、渲了哪些 section —— **知道**，而不是猜。

## ❗2026-08-26 换过一次信号（review §5.10 R-1）

初版拿 `build_observed_numbers`（造数守卫的 observed 集合）当「工具有没有返回数据」的信号，
判 `empty` 要求它为空。**那个信号在这里是错的**：`build_observed_numbers` 把工具结果 JSON
里**任何一个数字**都算数——`row_count: 0`、`elapsed_ms: 412`、`status: 504`、错误串里的编号，
全都算。116 全库实测：**549 个有 section 落库的轮，`has_data=False` 的是 0 个**
⇒ `empty` 一次都打不出来，而这个标记存在的全部理由就是要量它。

⇒ 改成按 **tool_name 认结果形状**，问的是「这个工具**真的返回行/条目了吗**」。
造数守卫继续用 observed（它问的是「这个数字有没有出处」，那个口径对）——两个问题不同，
不该共用一个信号。教训归档：`feedback_new_classifier_needs_a_dry_run_on_real_data`。

## 本判据的立场：精度优先，宁可不下结论

三值：`substantive` / `empty` / `None`（未判定，落库为 NULL）。

**绝不能把海报、绘图演示、散文真报告判成空**——那比留着几份空报告严重得多。
所以拿不准一律 `None`，把不确定如实记成不确定，而不是伪装成确定。

已知会漏的一类（有意识的取舍）：**围绕「没有数据」产出结构化内容的**
（`cv_53ea95cfb793`）——`empty` 仍要求 payload 里没有结构化结论。放开它能多逮一批，
但会开始误伤「用上一轮上下文写出真结论」的轮，等有了真实分布再单独拍。

## 用法：先记录、后使用

本模块只负责判，**不改任何读侧行为**（报告库照旧显示）。
等标记积累够、拿真实数据量过准确率之后，再单独决定报告库要不要按它过滤。
反过来（先上过滤）就是拿用户当试验场。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CONTENT_SUBSTANTIVE = "substantive"
CONTENT_EMPTY = "empty"

#: Three-valued tool-result signal. `unknown` means no recognizable fetch/compute tool ran this turn,
#: so there is no basis to claim tools returned nothing.
TOOL_CONTENT = "content"
TOOL_EMPTY = "empty"
TOOL_UNKNOWN = "unknown"


def _visual_kinds() -> frozenset[str]:
    """有这些段 ⇒ 这一轮交付了看得见的东西（图/物料/表）。**第一优先**：
    海报、设计稿、matplotlib 演示都可以一个数字都没有，按数字判会把这一整类误杀。

    ★复用 `report_tools._FIGURE_KINDS`，**不再维护第二份**（review §5.10 R-2）：
    初版自己抄了一份 `{"chart","image","design","table"}` —— `table` 在本系统**不存在**
    （canvas 段的表格 kind 是 `data_table`，`table` 只是 export IR 的 block kind，另一套词表），
    同时漏掉了 `data_table` / `datarc_iframe` / `html_artifact` /
    `external_iframe`。同族教训：`feedback_one_concept_many_implementations`。
    延迟 import 是为了不给这个纯判据模块拉进 report_tools 的整条依赖链。
    """
    from bowhead_core.agents.report_tools import _FIGURE_KINDS

    return frozenset(_FIGURE_KINDS)


#: Nonempty payload keys indicate structured conclusions, but alone cannot establish substantive content:
#: they may describe lack of data (see cv_53ea95cfb793 in module docstring).
_STRUCTURED_KEYS = ("kpis", "insights", "recommendations")

#: Fetch/compute tools: their results carry content obtained this turn. Only these tools can answer
#: whether tools actually returned anything.
#: ⚠ Deliberately exclude `subagent_spawn` / `subagent_return`: these are model self-reports,
#: not tool data. Treating self-report as evidence is the `project_chat_fabricates_when_payload_empty`
#: hole (model claims enter fabrication guard's observed set, making repetition count as provenance).
#: ⚠ Also exclude `bi_schema` / `skill_read` / `skill_list` / `config_read`: they return metadata.
_DATA_TOOLS = frozenset(
    {
        "bi_query",
        "bi_fetch_rows",
        "bi_breakdown",
        "bi_ask_question",
        "kb_search",
        "web_search",
        "web_fetch",
        # Exclude web_map: links alone are metadata (like bi_schema).
        "web_extract",
        "web_crawl",
        "web_deep_research",
        "python_sandbox",
        "skill_invoke",
    }
)

#: Output tools: successful calls mean this turn placed actual figures/material/embeds on the canvas.
#:
#: ⚠ Deliberately exclude `write_report` (caught on 2026-08-26, round 6-e): it writes prose,
#: and prose truthfully explaining no data is exactly what `empty` should mark. Counting it as output
#: makes every authored turn `content`, structurally disabling `empty` on the post-batch-3 main path again.
#: Same mistake as R-1. Observed cv_beb417ab26e0 (meta/narrative/sources) literally said
#: "该工作区不覆盖任何人力资源/员工相关指标…以下内容均不包含推测或编造的数据".
#:
#: ⚠ Retain `canvas_add_insight` intentionally as a boundary case: insight cards are structured conclusions;
#: cards about lack of data are the deliberate known false negatives in the module docstring
#: (cv_53ea95cfb793). Allowing this now would mark them empty too; not yet appropriate.
_PRODUCED_TOOLS = frozenset(
    {
        "add_chart",
        "add_image",
        "canvas_create_design",
        "canvas_add_insight",
        "canvas_add_html_artifact",
        "canvas_add_external_iframe",
        "canvas_add_spec_block",
    }
)


def _nonempty_list(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) > 0


def _nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _bi_returned_rows(result: dict[str, Any]) -> bool:
    """BI 三种真实形状（取自 `tool_result_preview.build_preview_for_tool` 的同一套判据）：

    - 批量：``{"results": [{ok, sample_rows|rows|metrics, ...}, ...]}``
    - 指标：``{"type": "metric", "metrics": [...]}``
    - 行：``{"rows"|"sample_rows": [...]}``
    """
    subs = result.get("results")
    if isinstance(subs, list):
        return any(
            isinstance(s, dict)
            and s.get("ok", True)
            and (_nonempty_list(s.get("sample_rows")) or _nonempty_list(s.get("rows")) or _nonempty_list(s.get("metrics")))
            for s in subs
        )
    return _nonempty_list(result.get("rows")) or _nonempty_list(result.get("sample_rows")) or _nonempty_list(result.get("metrics"))


def _sandbox_produced(result: dict[str, Any]) -> bool:
    """沙箱：跑成功 **且** 留下了 stdout / 产物。`ok=False` 一律算没产出。"""
    if not result.get("ok"):
        return False
    return _nonblank(result.get("stdout")) or _nonempty_list(result.get("outputs")) or _nonempty_list(result.get("output_artifacts"))


def _result_has_content(tool_name: str, result: Any) -> bool:
    """这一次工具调用**真的返回内容了吗**。认不出形状时保守返 `False`
    （调用方只会因此少下一个 `substantive`，不会多下一个 `empty`——
    `empty` 那条另有 `_DATA_TOOLS` 跑过的前提）。"""
    if not isinstance(result, dict):
        return False
    if result.get("error") and not result.get("ok"):
        return False
    if tool_name.startswith("bi_"):
        return _bi_returned_rows(result)
    if tool_name in ("python_sandbox", "skill_invoke"):
        return _sandbox_produced(result)
    if tool_name == "kb_search":
        # 116's real key is `chunks`; accept preview's legacy `hits` too.
        return _nonempty_list(result.get("chunks")) or _nonempty_list(result.get("hits"))
    if tool_name == "web_search":
        # Official compat interface intentionally returns summary without sources; that is content too.
        return _nonempty_list(result.get("results")) or _nonblank(result.get("summary"))
    if tool_name == "web_fetch":
        return _nonblank(result.get("text")) or _nonblank(result.get("content")) or _nonblank(result.get("markdown"))
    if tool_name == "web_extract":
        return any(_nonblank(page.get("content")) for page in result.get("pages") or [] if isinstance(page, dict))
    if tool_name == "web_crawl":
        return any(_nonblank(page.get("excerpt")) for page in result.get("pages") or [] if isinstance(page, dict))
    if tool_name == "web_deep_research":
        return _nonblank(result.get("report"))
    return False


def _walk_invocations(tool_invocations: Any) -> list[tuple[str, Any]]:
    """Collect tool names and results, including nested invocations.

    Dispatch moves temporary context results into invocation.nested. Traverse them
    here instead of requiring every caller to flatten first; repeated objects do not
    change the content conclusion.
    """
    out: list[tuple[str, Any]] = []
    for inv in tool_invocations or []:
        out.append((str(getattr(inv, "tool_name", "") or ""), getattr(inv, "result", None)))
        for nested in getattr(inv, "nested", None) or []:
            out.append((str(getattr(nested, "tool_name", "") or ""), getattr(nested, "result", None)))
    return out


def tool_content_signal(tool_invocations: Any) -> str:
    """本轮工具到底返没返内容 → `content` / `empty` / `unknown`。

    - `content`：至少一个取数/计算工具返回了行/条目，或至少一个产出工具写成功了。
    - `empty`：**跑过**取数/计算工具，但一个都没返回内容，且没有任何产出工具成功。
    - `unknown`：一个认得出的工具都没跑过 —— 没有资格说「工具什么都没返」。
    """
    seen_data_tool = False
    for name, result in _walk_invocations(tool_invocations):
        if name in _PRODUCED_TOOLS and isinstance(result, dict) and result.get("section_id") and not result.get("skipped"):
            return TOOL_CONTENT
        if name in _DATA_TOOLS:
            seen_data_tool = True
            if _result_has_content(name, result):
                return TOOL_CONTENT
    return TOOL_EMPTY if seen_data_tool else TOOL_UNKNOWN


def assess_canvas_content(
    *,
    payload: Any,
    emitted_kinds: Any,
    tool_signal: str,
) -> tuple[str | None, str]:
    """→ `(status, reason)`；`status` 为 `None` 表示**不下结论**。

    Args:
        payload: 本轮聚合后的 AnalysisPayload（compose 的输入）。
        emitted_kinds: **这一轮落在画布上的可见 section kind** —— 组稿器自己渲的
            **加上** peer 亲笔写的。⚠ 不能只传组稿器的返回值：亲笔路的图/表由 peer 直接
            调 canvas 工具写，收尾器的 `written` 词表里一个 `_FIGURE_KINDS` 都没有
            （116 dogfood `t_c5c57f197cd7` 实证）。
        tool_signal: `tool_content_signal()` 的产物（`content`/`empty`/`unknown`）。
            **不是**造数守卫那个 observed 数字集合 —— 那个信号在这里恒为真，见模块 docstring。

    判定顺序（精度优先，先判"有"再判"空"）：

    1. 渲出了图/物料/表 ⇒ `substantive`（海报与绘图轮走这条，不看数据）
    2. payload 有结构化结论 **且** 工具真返了内容 ⇒ `substantive`
    3. 既无视觉产出、payload 也无结构化结论、且**跑过取数工具但一条都没返** ⇒ `empty`
    4. 其余 ⇒ `None`（拿不准，交给后续迭代）
    """
    kinds = {str(k) for k in (emitted_kinds or ())}

    if kinds & _visual_kinds():
        return CONTENT_SUBSTANTIVE, "visual_output"

    structured = _has_structured(payload)
    if structured and tool_signal == TOOL_CONTENT:
        return CONTENT_SUBSTANTIVE, "structured_with_data"

    if kinds and not structured and tool_signal == TOOL_EMPTY:
        # No visuals or structured conclusions, fetch tools ran but returned nothing: this turn has nothing deliverable.
        # Typical: repeated BI errors / nonexistent dimension values / all upstream steps empty.
        #
        # ⚠ Require nonempty `kinds`: `empty` means rendered output contains only explanatory content,
        # not that information is unavailable. Without this gate, missing emitted_kinds from callers
        # (wiring bug / incomplete compose) yields a confident wrong conclusion,
        # another case where defaults hide missed wiring as a negative result.
        #
        # ⚠ `tool_signal` must equal `empty`, not merely differ from content. `unknown`
        # (no recognizable tool ran) also forbids conclusions: uncertainty is not absence.
        return CONTENT_EMPTY, "no_data_no_output"

    # Remaining cases are uncertain; record undecided honestly:
    #   · structured without tool data: may be structured content about no data
    #   · tool content without structured output: may be real prose reports, fallback or unrelated data
    #   · tool_signal == unknown: no recognizable fetch tool ran, so cannot judge
    return None, "undecided"


def _has_structured(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(payload.get(k) for k in _STRUCTURED_KEYS)

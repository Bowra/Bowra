"""模型面投影 —— 「模型能看见工具结果里的什么」的**唯一**知识点。

## 为什么需要这一层

在此之前，答案是「工具往结果 dict 里塞了什么，模型就看见什么」——`subagent._serialize`
就是一句 `json.dumps(inv.result)`，零过滤。于是"模型面"是**被每个工具隐式定义**的，
没有任何一处能回答「模型到底看得见哪些标识」。

artifact-addressing 调研（`docs/sprints/artifact-addressing-2026-08-19/research.md`）
量出来的后果：116 全量 282 次沙箱取数失败里 **≈95% 是"选错了命名空间 / 自己造一个"**，
而模型眼前同时摆着六套标识（`art_xxx` / 简单整数 / 文件名 / INPUTS key / `sec_xxx` /
`call_id`）。离线 2×2 对照进一步把有效成分钉死了：同一个任务、同一个模型，
**清单里多给一个"看起来也像路径的串"，正确率 100% → 85.9%；把它拿掉就回到 100%**。

⇒ 有效成分不是"标识符长什么样"，是**同一个槽位有几个看起来合理的候选串**。
所以要治它，就得有一处能说清"模型面到底有什么"——就是本模块。

## 边界（重要）

**只投影送进 LLM 上下文的那一份，绝不改 `inv.result` 本身。** 后端与用户面仍要读原字段：
`canvas_composers/mappers.py` 拿 `artifact_filename` 渲 Code tab 的 `-- artifact:` 行
（那是**给用户看的**）。删字段会连带打断它；投影不会。

## 加新条目前想清楚

删一个键的判据是「模型**不需要**它来完成工作，而它又是另一个可以填进某个槽位的候选串」。
不是「这个键很长」，也不是「这个键是内部的」。
"""

from __future__ import annotations

import os
from typing import Any

# ── Per-tool backend-only keys: remove from the model's view during projection ────────────────────────
#
# **Original artifact filenames**: unnecessary for the model (fetch uses `INPUTS[key]['path']`,
# references use artifact_id / simple integers), but another path-like string. On production 116,
# they caused 22 cases of Chinese filenames used as paths. Batch 2b's `origin=` (original producing query)
# now describes the data, more fully than filenames (origin stripped of spaces/lowercased plus
# hash suffix).
#
# ⚠ User-uploaded / KB files are **excluded**: user-chosen filenames anchor recognition;
# renaming/hiding them prevents users recognizing their files (2026-08-19 decision 10). They do not use bi_* tools,
# naturally remaining unaffected by this table.
_BACKEND_ONLY_KEYS: dict[str, frozenset[str]] = {
    # `_debug_explain_message` and model-facing `query_explain` are two forms of the same content
    # (prose vs structured); including both spends budget twice. Keep prose for backend troubleshooting.
    # Datarc ids (`m1` / `d54` cut props, task uid) are not accepted by any tool; keep them for audit only.
    # The cut's named tokens are withheld too (see `_executed_query_first`).
    "bi_query": frozenset({"artifact_filename", "_debug_csv_filename", "_debug_explain_message", "_debug_cut_props", "_debug_task_uid"}),
    # `bi_fetch_rows` entry removed with tool retirement on 2026-09-05: it is no longer registered,
    # so tool_name-based projection could never match it. All row-level fetches now return under bi_query.
}

# Artifacts from these sources **belong to users**: filenames are user-chosen recognition anchors
# and must stay model-visible (2026-08-19 decision 10). Others (bi_* / python_sandbox / canvas_export …)
# are machine-produced: filenames add no model value, only another candidate string for a path slot.
# Align with `artifact_store`'s `source` values. **List and Lead pinned block share this definition**;
# duplicating it then testing parity repeats the sprint's diagnosed one-concept/many-implementations antipattern.
USER_OWNED_SOURCES = frozenset({"upload", "kb_upload", "kb_bundle_upload"})

_FLAG_ENV = "BOWHEAD_MODEL_VIEW_PROJECTION"


class ModelText(str):
    """A projection that is already model text: the serializer sends it verbatim.

    Any other projection is JSON-encoded, which escapes newlines and quotes.
    Only a projector that renders complete readable text returns this type.
    """


def enabled() -> bool:
    """默认 ON。`BOWHEAD_MODEL_VIEW_PROJECTION=0` 可即时关掉（不必回滚镜像）。

    每次读 env（同 `psi_flags` 的取向）——/admin/flags 改了立刻生效。
    """
    return os.environ.get(_FLAG_ENV, "1") != "0"


# ── Chat inline figures: add an anchor hint to chart-producing tool results ────────────────────────────
#
# Previously this module only removed backend keys. This addition still belongs to the same knowledge boundary,
# what tool-result content models can see, so it lives here as **the sole convergence point**:
#   · Figure creation has multiple return sites (chart / image / table / kpi in `canvas_tools`,
#     plus two D6 dedup hits); changing each duplicates one concept across many sites.
#   · **Peers** write chat bodies (`_short_answer = render_chat_answer_markdown(payload)`),
#     with three prompt sources (manifest `compute.md`, team custom, user custom);
#     changing each also duplicates work and requires another update for every new custom agent.
#   · **Every agent necessarily reads** tool results, independent of prompt source.
# Regression check evidence: giving the list to Lead was useless; `_chat_direct_delivery` short-circuits Lead
# in chat turns, with no writing iteration (t_6787e8159e94 / t_752998836ac7 both had 0 anchors).
#
# Add hints only when a section was **actually created** (`section_id` in result), never for failure/skip results.
_FIGURE_ANCHOR_TOOLS = frozenset(
    {
        "add_chart",
        "canvas_add_kpi_row",
        "canvas_add_data_table",
        "canvas_add_table",
        "add_image",
    }
)

_FIGURE_ANCHOR_KEY = "inline_hint"


def _with_figure_anchor_hint(result: Any, *, anchor_id: str | None = None) -> Any:
    """Add figure-anchor guidance without mutating the original result.

    Prefer the model-facing anchor_id; otherwise use the real section ID, consistent
    with the shared allowlist translation boundary.
    """
    if not isinstance(result, dict):
        return result
    sid = result.get("section_id")
    if not sid or result.get("error"):
        return result
    if _FIGURE_ANCHOR_KEY in result:
        return result
    shown = anchor_id or sid
    out = dict(result)
    out[_FIGURE_ANCHOR_KEY] = (
        f"若这一轮以 chat 答案交付：在正文里**独占一行**写 [figure:{shown}]，"
        "这张图就渲在那一段旁边（写进 exec_summary 或某条 insight 的 narrative 均可）。"
        "不写 = 图落在答案末尾，不会丢。报告轮不用写这个，照常 write_report。"
    )
    return out


# ── bi_query: the hint and what ran lead the result; the cut stays backend-only ─────────────────────
#
# `_preflight_hint` was the last key, after the rows, the chart catalog and the sandbox usage note. Regression check
# (2026-11-15, 1.7.38): compute read the rows and the report dates, then went straight to the sandbox without ever
# addressing the hint. The hint states what the rows are not, so the model should read it before the rows.
#
# 116 re-check on 1.7.43 (2026-11-15): the model saw two parses of each query. `_parse.tokens` comes from Datarc's
# separate /data/cut/ call, made before a model is chosen, and bound 女装 to 种类大类 word by word; `query_explain`,
# Datarc's report of the query it executed, showed that the filter was dropped only by lacking a `filters` key.
# deepseek-flash delivered all-store 月坪效 as 女装 in two of three runs and quoted the binding ("种类大类=女装",
# "女装是种类大类维度下的取值"). The model now sees only what ran: the tokens and the internal gap record are withheld
# (the handler and the trimmer still read them to write the hint), the report follows the hint, and a structured
# report without `filters` shows `"filters": []`, since Datarc omits the keys that took no effect.
_HINT_KEY = "_preflight_hint"
_REPORT_KEYS = ("query_explain", "query_explain_text")
_PARSE_KEY = "_parse"
_BACKEND_ONLY_PARSE_KEYS = frozenset({"tokens", "fields_not_in_report"})


def _executed_query_first(result: Any) -> Any:
    """Lead a bi_query result, and each batched sub-result, with its hint and the executed-query report."""
    if not isinstance(result, dict):
        return result
    lead = {key: result[key] for key in (_HINT_KEY, *_REPORT_KEYS) if key in result}
    out = {**lead, **{key: value for key, value in result.items() if key not in lead}}
    report = out.get("query_explain")
    if isinstance(report, dict) and report and "filters" not in report:
        out["query_explain"] = {**report, "filters": []}
    parse = out.get(_PARSE_KEY)
    if isinstance(parse, dict):
        out[_PARSE_KEY] = {key: value for key, value in parse.items() if key not in _BACKEND_ONLY_PARSE_KEYS}
    if isinstance(out.get("results"), list):
        out["results"] = [_executed_query_first(item) for item in out["results"]]
    return out


def _strip(node: Any, keys: frozenset[str]) -> Any:
    """递归摘键。批量 bi_query 的结果是 `{"results": [ {...}, ... ]}`，
    单查询是扁平 dict —— 两种形状都要覆盖，所以走递归而不是只看顶层。"""
    if isinstance(node, dict):
        return {k: _strip(v, keys) for k, v in node.items() if k not in keys}
    if isinstance(node, list):
        return [_strip(v, keys) for v in node]
    return node


def project_tool_result(
    tool_name: str | None,
    result: Any,
    *,
    figure_anchor_id: str | None = None,
) -> Any:
    """返回**给模型看的那一份**。原对象不变（后端/用户面继续读原字段）。

    未登记的工具原样返回——本表是显式 opt-in，不做"猜哪些键像内部的"。
    """
    if not tool_name or not enabled():
        return result
    if tool_name == "skill_read":
        from bowhead_core.skill_text import render_skill_read_result, skill_via_lead_enabled

        if skill_via_lead_enabled() and (text := render_skill_read_result(result)) is not None:
            return ModelText(text)
    if tool_name == "present_artifacts" and isinstance(result, dict):
        result = dict(result)
        for key in ("artifacts", "output_artifacts"):
            if isinstance(result.get(key), list):
                result[key] = [
                    {k: v for k, v in item.items() if k not in {"title", "description"}} if isinstance(item, dict) else item for item in result[key]
                ]
        if result.get("ok"):
            if result.get("output_artifacts"):
                # Declarations contain authored labels; expose one canonical file table.
                result.pop("artifacts", None)
            result["evidence_guidance"] = (
                "Presentation labels are model-authored, not independent evidence. Use native diagnostics only for their stated checks. "
                "Not checked does not mean absent. runtime_checked=false describes this checker, not prior network activity."
                " Copy each file's ID, name, MIME and size from the same output_artifacts entry. "
                "File actions display only the canonical filename, never caller-authored titles or descriptions. "
                "The native file list provides download links. Do not repeat raw artifact IDs in prose unless explicitly requested."
            )
    if tool_name in _FIGURE_ANCHOR_TOOLS:
        result = _with_figure_anchor_hint(result, anchor_id=figure_anchor_id)
    if tool_name == "bi_query":
        result = _executed_query_first(result)
    keys = _BACKEND_ONLY_KEYS.get(tool_name)
    if not keys:
        return result
    return _strip(result, keys)

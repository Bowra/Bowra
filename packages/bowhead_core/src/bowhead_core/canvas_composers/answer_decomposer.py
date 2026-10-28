"""Answer decomposer — 把 Lead/Analyst 的 final markdown 直接拆成 canvas sections.

**为什么有这个**：
- Lead 已经（或 subagent 已经）写好完整 markdown 答案（放在 chat bubble）
- 原来 compose_answer 不看这个答案，让 narrator LLM 另写一遍 → canvas 和 chat 脱节
- 结果经常是 "chat 写了一大堆，canvas 几乎空"

**这里做的**：
1. 解析 markdown：headings / tables / bullets / blockquotes / links / "**标签**: 数值"
2. 按语义映射到 section kind：
    - `## 标题`           → section (h2)
    - `**X**: 14.16亿`     → kpi_row（若聚合出 ≥ 1 个 cell）
    - markdown 表格        → data_table
    - `> 引用 / ⚠/💡 开头段` → callout
    - 有序列表 with 动词   → next_list
    - 散文段落             → narrative
3. 跨对工具结果的 `authorized_numbers`：输出里的数字如果不在容差范围内 → 标记
   `unverified: true` 在 meta，让前端可视化
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import TYPE_CHECKING, Any

from bowhead_core.types import ToolInvocation

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# citation-audit M3 removed `_INLINE_BIND_METRIC = "bowhead_inline_bind_total"`:
# its sole emitter `_bind_inline_num_sources` (gen2 autowrap) was deleted in this batch, orphaning the constant
# (Ruff does not check module-level constants, so the gate stayed green). The gen3 equivalent is `report_numbered_bound`,
# emitted inside `citation_marks.bind_to_numbered`, the sole binding seam, automatically instrumenting all four callers
# (implementation review deletion-safety P2).
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
# Dedicated weak-source (sandbox stdout) extraction: consume thousands-grouped numbers whole to avoid fragments (review C2).
_WEAK_STDOUT_NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")
_DATARC_HOST_RE = re.compile(r"(demo\.datarc|openapi\.datarc|datarc\.cn)", re.I)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


# Tool machine names -> Chinese (aligned with frontend tool-labels.ts, for `数据来源` callouts).
_TOOL_LABEL_CN: dict[str, str] = {
    "bi_ask_question": "问 BI",
    "bi_attribute": "归因分析",
    "bi_list_measures": "列出指标",
    "bi_list_dimensions": "列出维度",
    "bi_get_card": "取图表",
    "bi_get_cards": "批量取图表",
    "bi_explain": "解读数据",
    "skill_list": "列出 Skill",
    "skill_read": "读 Skill",
    "skill_invoke": "执行 Skill",
    "subagent_spawn": "委派智能体",
}


# ─────────────────────────────────────────────────────────────
# Tool results -> trusted numeric values / chart URLs.
# ─────────────────────────────────────────────────────────────
# #310: number -> source call_id mapping, upgrading narrative.markdown [num:V:U]
# to [num:V:U:src:S]. Frontend citation_plugin already decorates the [num:V:U:src:S] format.
#
# CTO review P1-R1.1 fix:
# 1. Walk only known data fields (metrics/rows/measures/cells/value/content), not the entire object,
#    avoiding ID/timestamp/seq and other numeric metadata noise as source data.
# 2. When multiple invocations contain the same value, SKIP (set None) rather than choosing the first, preventing false-positive popovers
#    (a percentage 0.21 in five invocations is safer without a source binding than with a wrong one).
_DATA_FIELD_KEYS = {
    "value",
    "content",
    "metrics",
    "rows",
    "measures",
    "cells",
    "measure_value",
    "measure_current",
    "measure_previous",
    "delta",
    "previous",
    "current",
    "row_count",
    "total_rows",
    # Sprint χ-3.1/3.3 P0-C2: bi_query batch shape `{results: [{metrics, ...}]}` —
    # add `results` so walk descends into batch sub-entries, where `metrics` (KPI
    # high-density single values, already in this set) gets reached naturally.
    # **NOT adding `sample_rows`**: the ι-15 dogfood lesson was that sample_rows is raw row data
    # (low information density, high cross-inv collision rate) → ambiguous_drop
    # invariant dropped everything, reducing bind rate 82%->17%. Adding it here causes the same class of regression.
    # Batch numbers from sample_rows reach citation via narrative cite mechanism
    # (PRD §10.1 binding range markers), not via v2s map.
    "results",
    # Sprint ι dogfood-1 (D) tried adding outputs/preview/columns here to walk
    # sandbox preview rows into the map. Reverted (2026-05-09 r4 dogfood):
    # the BI workspace pulls 1000+ rows per query so adding sandbox cells
    # multiplied collision count → ambiguous-window invariant dropped
    # everything → bind rate 82% → 17%. Sandbox-derived values stay out of
    # the map for now; binding range markers (PRD §10.1) covers the user need.
}


class _ValueSourceMap(dict):
    """dict[float, str] + 类别元数据（review F2）。

    `strong_keys` = strong 走查**见过的全部值**（含多源歧义被 drop、未入图的）。
    `_resolve_value` 用它做窗口强优先：精度窗口里只要有 strong 值，weak
    （sandbox stdout）候选一律出局——否则近邻 weak 键会把既有 strong 绑定
    挤成歧义（丢绑定），或把 strong 歧义值经窗口翻案给 sandbox（错绑定）。
    普通 dict（无该属性）走旧行为，老调用面零变化。
    """

    strong_keys: frozenset[float] = frozenset()


# Batch 5ⓑ: flat sandbox entry-point tool names. A separate constant makes every name-based sandbox recognition site
# searchable with grep (same convention as subagent.py / resource_access.py).
_FLAT_SANDBOX_TOOL = "python_exec"


# Distinguish the runner's two whole-run failure classes (review R2-1): `_fail_data_materialization`
# marks a completed run ok=False/exit_code=1 when **artifacts** cannot materialize (file over 50MB, upload failure,
# or missing declared deliverable), while the runner itself states that run success remains independent from
# artifact presence. In this failure class, **user code completed**, and stdout numbers are final values.
# ⚠ The criterion depends on runner reason text (three `_fail_data_materialization` callers in `runner.py`);
# text drift treats these as genuine crashes, a conservative fallback that does not misbind sources.
_SANDBOX_MATERIALIZATION_ERROR_RE = re.compile(
    r"^(?:sandbox \S+ (?:exceeds Data artifact limit|Data artifact upload failed)"
    r"|declared deliverable outputs were not produced and persisted:.*)$",
)


def _sandbox_run_failed(result: dict[str, Any]) -> bool:
    """沙箱**真的跑挂了**吗（D17 判据 + review R2-1 的物化失败豁免）。

    扁平/skill-runtime 入口把失败编码成 **外层 `ok=True` + `result_json.ok=False`**
    且 `inv.error is None` —— 只看 `inv.error` 的调用方会把「崩溃前打印的中间量」
    当成可信来源（review F3）。缺 ok/exit_code（合成 fixture）→ 视为成功。

    但**产物物化失败不算跑挂**（review R2-1）：一次算完并打印了全部派生值、同时又写了
    个 60MB CSV 的运行会被 runner 标 ok=False，若照 D17 原样跳过，「stdout 派生值裸奔」
    这个 e2 要修的回归就会静默复发。所以 ok=False 时再看 error：**全部片段都是物化
    reason ⇒ 判成功**；掺了别的（traceback 等）或带 `cause`（executor 不可达一类环境
    失败）⇒ 判失败。
    """
    inner = result.get("result_json")
    inner = inner if isinstance(inner, dict) else result
    if inner.get("cause") or result.get("cause"):
        return True  # Environment/backend failures: stdout is usually empty and should not serve as a source.
    exit_code = inner.get("exit_code")
    if exit_code is None:
        exit_code = result.get("exit_code")
    flagged = result.get("ok") is False or inner.get("ok") is False or (exit_code is not None and exit_code != 0)
    if not flagged:
        return False
    err = str(inner.get("error") or result.get("error") or "").strip()
    if not err:
        return True  # Failure without a reason -> conservatively treat as a genuine failure.
    parts = [p.strip() for p in err.split(";") if p.strip()]
    return not all(_SANDBOX_MATERIALIZATION_ERROR_RE.match(p) for p in parts)


def _sandbox_output_artifact_id(result: dict[str, Any]) -> str | None:
    """沙箱 **stdout 产物** 的 id（`result_json.outputs[]` 里 `data_role == "stdout"`）。

    ★判据是 `data_role`，不是文件名：`runner._upload_artifact` 返的记录只有
    `{name, artifact_id, mime, size}` —— **没有 `filename` 键**（本仓其余消费方
    一律 `spec.get("filename") or spec.get("name")` 兼容两者），而
    `_materialize_data_outputs` 会另外盖一个 `record["data_role"] = "stdout"`。
    2026-08-29 review F1：初版读 `filename` ⇒ 生产上永不命中、修复整个是死的，
    而单测因为 fixture 自己发明了 `filename` 键照样全绿。

    **只认 stdout，不拿别的产物顶替**：weak 值是从 stdout 文本抽的，指向 `out.csv`
    之类只是「看着有出处、点开对不上」——CSV 预览只给前几行，那个数未必在里面。
    本模块既有原则是「不绑 src 比绑错安全」（见 `_build_value_to_source_map`
    docstring），这里同样取严：没有 stdout 产物就返 None，调用方退 call_id。
    ⚠ 该分支不是死码：`runner.py` 虽在 stdout 非空时就落 artifact，但 stdout
    超 `_MAX_OUTPUT_BYTES` 时会跳过——而「输出很多数字」正是 weak 臂最活跃的场景。

    守卫与 strong 走查同款（str + `art_` 前缀），防乱形 id 变成垃圾来源条目。
    """
    rj = result.get("result_json")
    records: list[Any] = []
    # Top-level `output_artifacts` projects `result_json.outputs[]` in tools.py (preserving
    # artifact_id/name/data_role, `_output_artifacts_from_result_json`). Both originate from the same data;
    # read both to cover flat-envelope and raw-runner result shapes.
    top = result.get("output_artifacts")
    if isinstance(top, list):
        records.extend(top)
    if isinstance(rj, dict) and isinstance(rj.get("outputs"), list):
        records.extend(rj["outputs"])
    if not records:
        return None

    def _aid(rec: Any) -> str | None:
        if not isinstance(rec, dict):
            return None
        aid = rec.get("artifact_id")
        return aid if isinstance(aid, str) and aid.startswith("art_") else None

    # Review R2-3: **search the whole run for authoritative data_role first, then fall back to filename heuristics**. A single element-wise pass
    # lets a user-authored `sandbox-stdout.txt` match before the actual artifact (the runner scans workdir files first, then appends
    # real stdout last under the renamed `sandbox-stdout-data-1.txt`).
    for rec in records:
        aid = _aid(rec)
        if aid and str(rec.get("data_role") or "") == "stdout":
            return aid
    for rec in records:
        aid = _aid(rec)
        if aid and "sandbox-stdout" in str(rec.get("name") or rec.get("filename") or "").lower():
            return aid
    return None


def _build_value_to_source_map(
    invs: list[ToolInvocation],
) -> dict[float, str]:
    """限 walk inv.result 已知数据字段, 抽 numeric value 关联溯源 id。

    溯源 id 粒度 (飞书07.06 #11 根修): 优先**子结果 artifact_id**, 兜底 call_id。
    bi_query 批量结果 `{results:[{artifact_id,…},…]}` 一个 call_id 名下 N 张表 —
    绑 call_id 分不出数字来自哪张, 下游 (FE sourceLookup popover / Phase-F
    resolve_in_text 的 [N] 分配 / source_ref_aggregator) 都只能猜"批次第一张",
    prod 实证指错表 (s_8a063ae8cd8e: 同比变化率的值指到一级分行 csv)。
    artifact id 在 TurnSourcesRegistry `_id_to_index` 与 FE sources-entry
    alias 两侧都有精确条目, 所以 walk 进入带 artifact 的 dict 时把 src_id
    切换成该 artifact, 值就绑到具体那张表。

    返回 dict[float_value, source_id]. value 多源出现 → 该 entry 置 None
    (caller 取 None 视为不绑 src, 比绑错 src 安全)。

    设计取舍 (P1-R1.1):
    - 限 walk: 只走 _DATA_FIELD_KEYS 命中的 dict path, 跳 id/timestamp/seq 噪音
    - 多源 collision: ambiguous → drop, 防 popover 跳错 source
      (批量内两张表撞同值 → drop — 老行为是整批绑 call_id, 假绑定)
    """
    raw: dict[float, set[str]] = {}

    def walk(v: Any, src_id: str, *, in_data_path: bool) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            if not in_data_path:
                return
            raw.setdefault(float(v), set()).add(src_id)
        elif isinstance(v, str):
            if not in_data_path:
                return
            for m in _NUM_RE.findall(v):
                try:
                    raw.setdefault(float(m), set()).add(src_id)
                except ValueError:
                    continue
        elif isinstance(v, dict):
            # Feishu 07.06 #11: a result node with its own artifact assigns that subtree's values to that table.
            # Trust internal reference field `_ref_artifact_id` directly; require the art_
            # prefix for `artifact_id` to avoid binding row values to arbitrary IDs from a coincidentally named data column.
            sub_aid = v.get("_ref_artifact_id")
            if not (isinstance(sub_aid, str) and sub_aid):
                _cand = v.get("artifact_id")
                sub_aid = _cand if isinstance(_cand, str) and _cand.startswith("art_") else None
            if sub_aid:
                src_id = sub_aid
            for k, val in v.items():
                # Recurse only below known data fields (bounded scope).
                next_data = in_data_path or k in _DATA_FIELD_KEYS
                walk(val, src_id, in_data_path=next_data)
        elif isinstance(v, list):
            for val in v:
                # List items inherit path state (each row inside rows/cells is on a data path).
                walk(val, src_id, in_data_path=in_data_path)

    for inv in invs:
        if inv.error or inv.result is None:
            continue
        cid = getattr(inv, "call_id", "") or ""
        if not cid:
            continue
        # Start top-level in_data_path=False; switch to True only when a dict enters _DATA_FIELD_KEYS.
        # This excludes top-level inv.result metadata such as id/seq/timestamp from traversal.
        walk(inv.result, cid, in_data_path=False)

    # Collapse values with multiple sources: drop (no source binding is safer than a wrong one).
    out: dict[float, str] = {}
    for value, sids in raw.items():
        if len(sids) == 1:
            out[value] = next(iter(sids))

    # ── Source-accessibility presentation e2: sandbox stdout as a **weak-class** value source ──────
    #
    # 116 measurement (expression-discovery 2026-08-28): class D, with narrative numbers that ran through bake
    # but received no [N] binding, accounted for 31% of paragraphs. The main cause was LMDI/price-volume sandbox calculations
    # whose derived values existed only in stdout, deliberately excluded by _DATA_FIELD_KEYS (ι-15 lesson:
    # raw sample_rows/preview values in the map caused ambiguity drops, reducing binding rate 82%->17%).
    # Avoid repeating ι-15: weak sources **never modify the strong map** and add bindings only if all three conditions hold:
    #   ① Strong traversal never encountered the value, including ambiguity drops: real-data ambiguity must not be
    #      overruled by weak sources; ② the value is unique among weak sources (exactly one sandbox source);
    #   ③ the value is nontrivial (|v|≥100 or noninteger), excluding stdout row/ordinal noise.
    # This is structurally additive: no existing binding changes, so the ι-15 regression (new sources making a strong value
    # ambiguous and dropping it) cannot occur. Offline comparison (eight real 116 stdout runs) added 28 bindings, all
    # price-volume/customer-spend derived measures, with zero misbindings.
    weak: dict[float, set[str]] = {}
    # Review F9b: bridge keys are **second-class**. Actual values (strong or genuine weak, including ambiguity
    # drops) always win. Bridge keys fill only values absent from the genuine set; bridge-key collisions still drop.
    # Otherwise sandbox A's bridge key makes sandbox B's actual value ambiguous, losing an existing binding.
    bridged: dict[float, set[str]] = {}
    for inv in invs:
        if inv.error or not isinstance(inv.result, dict):
            continue
        # Same three batch 5ⓑ entry points, aligned with _extract_sandbox_tables: legacy pseudo-tool name /
        # skill_invoke(name="python_sandbox") / flat `python_exec`. Recognizing only
        # "python_sandbox" repeated the batch 5ⓑ blind spot: switching compute to the flat entry silently disabled
        # the entire weak branch (2026-08-29 device t_1c34b8e87ef9: volume/price contribution derived values had no citations,
        # leaving no inputs for the e2-r1 ten-thousand/hundred-million bridge; scenario comparisons are in the expression-discovery changelog).
        _tn = str(getattr(inv, "tool_name", "") or "")
        _args = inv.arguments if isinstance(getattr(inv, "arguments", None), dict) else {}
        if _tn not in ("python_sandbox", _FLAT_SANDBOX_TOOL) and not (_tn == "skill_invoke" and _args.get("name") == "python_sandbox"):
            continue
        # Review F3: **failed sandboxes are not sources**. Flat/skill-runtime entry points encode failure as
        # outer ok=True + result_json.ok=False with `inv.error is None`, completely bypassing
        # the `inv.error` guard above. Before reading nested shapes this path was unreachable; adding them introduced
        # pre-crash intermediate stdout values as citation sources. Reuse mappers' D17 criterion.
        if _sandbox_run_failed(inv.result):
            continue
        # Two generations of stdout shapes: flat/skill-runtime nests the actual result in result_json
        # ({ok, result_json: {stdout/output, outputs…}}), while the legacy direct shape is top-level.
        _rj = inv.result.get("result_json")
        _rj = _rj if isinstance(_rj, dict) else {}
        stdout = inv.result.get("stdout") or inv.result.get("output") or _rj.get("stdout") or _rj.get("output")
        if not isinstance(stdout, str) or not stdout.strip():
            continue
        # Review C6: use the same source-ID guard as strong traversal: trust `_ref_artifact_id` directly;
        # `artifact_id` must be a string starting with art_ (prevent dicts/malformed IDs in results from creating
        # garbage source entries). If neither qualifies, fall back to call_id, resolvable by inv_lookup.
        _raid = inv.result.get("_ref_artifact_id")
        if isinstance(_raid, str) and _raid:
            src_id = _raid
        else:
            _aid = inv.result.get("artifact_id")
            if isinstance(_aid, str) and _aid.startswith("art_"):
                src_id = _aid
            else:
                # e2-r2 second half: flat/skill-runtime artifact IDs are in `result_json.outputs[]`,
                # absent at top level. Without reading them, fallback uses call_id, which no resolver recognizes,
                # inevitably classifying ownership as `linked_unavailable`; opening the prose citation shows `该来源不可用`
                # (measured on 1.1.94 device t_1831b22ed3c1, exposed after e2-r2 actually bound values).
                # Prefer the stdout artifact: weak values come from stdout, making this the most precise attribution.
                # It is also an artifact from this turn, so the existing art_ resolver returns OWNED, **without adding authorization bypasses**.
                src_id = _sandbox_output_artifact_id(inv.result) or str(getattr(inv, "call_id", "") or "")
        if not src_id:
            continue
        # Review C2: stdout is **formatted text** (f"{v:,.0f}" thousands separators are common). Bare
        # `_NUM_RE` splits `1,234,567` into false values 234/567 in the map, producing incorrect attribution.
        # Extract the whole thousands-grouped form, then strip commas (same extraction approach as number_verifier).
        # Review F9: stdout is immutable, but this function rebuilds ~15 times per turn (after each spawn completion and
        # each bake segment). Memoize extraction on the invocation; recompute when setattr fails.
        vals = getattr(inv, "_weak_stdout_values", None)
        if vals is None:
            vals = []
            for m in _WEAK_STDOUT_NUM_RE.findall(stdout[:100_000]):
                try:
                    v = float(m.replace(",", ""))
                except ValueError:
                    continue
                # ③ Exclude trivial integers (row numbers/counts/ordinals) from weak sources. Judge by **original token shape**:
                # a decimal point in `13.0` implies a measured value (real unit-price 13.0 cases benefited in 116 offline comparisons),
                # whereas bare 7 / 42 are row/count noise.
                if "." not in m and abs(v) < 100:
                    continue
                vals.append(v)
            with contextlib.suppress(Exception):  # Recompute each time for slots/frozen objects.
                inv._weak_stdout_values = vals
        for v in vals:
            weak.setdefault(v, set()).add(src_id)
            # e2-r1: ten-thousand/hundred-million scaling bridge. Stdout often prints scaled values (-3068.5, ten thousand), while prose uses
            # the original (-3,068.5 ten thousand yuan -> -30,685,000). The scale gap defeats exact/window matching,
            # leaving price-volume derived values, which most need sources, uncited. Use the `万/亿` factors from _CN_UNITS
            # (same source as prose parsing to prevent drift, review F9c); **explicitly exclude percentage ÷100**
            # because low-entropy fractions misbind and repeated year-over-year rates flood paragraphs with citations; see expression-discovery
            # e2-r1 records. Review F9a: do not bridge fractions |v|<1 (such as 0.17): ×1e4 creates
            # rounded low-entropy keys like 1700.0, incorrectly assigning sandbox attribution to unrelated prose `1,700元`.
            if abs(v) < 1:
                continue
            for _f in _WEAK_SCALE_FACTORS:
                _ev = v * _f
                if abs(_ev) < 100 and float(_ev).is_integer():
                    continue
                bridged.setdefault(_ev, set()).add(src_id)
    for value, sids in weak.items():
        if value in raw:  # ① Weak sources never take over values seen by strong traversal, including ambiguity drops.
            continue
        if len(sids) == 1:  # ② Bind only values unique among weak sources; equal values across sandboxes still drop.
            out[value] = next(iter(sids))
    for value, sids in bridged.items():
        if value in raw or value in weak:  # Actual values, including ambiguity drops, always beat bridge keys.
            continue
        if len(sids) == 1:
            out[value] = next(iter(sids))
    # Review F2: ① blocks only **exact equality**. Precision-window matching can still let nearby weak keys interfere with strong values
    # (losing bindings/overruling ambiguity). Forward the complete strong-value set with the map to _resolve_value
    # so strong values win within the window.
    m = _ValueSourceMap(out)
    m.strong_keys = frozenset(raw)
    return m


# Sprint ι (PRD §10.1.1.2 + dogfood-trace 2026-05-09 batch expansion):
# Match precedence: currency strip → thousand-sep strip → CN suffix → EN suffix → percent.
# Single source of safe-fail truth (regex's role = admit; parser's role = canonicalize).
#
# Coverage rationale: LLM marker emit varies wildly across prompts/models. Live
# trace observed: `[num:680万]`, `[num:11.18亿元]`, `[num:6.8M]`, `[num:0.685:percent]`,
# `[num:5:bp]`. Tables below cover the practical surface; entries are case-explicit
# (no .lower() collapse) because some tokens (`M` vs `m` in finance) carry the same
# semantic but operating on the LLM-emitted string keeps the lookup deterministic.
_CN_UNITS = {
    "亿": 1e8,
    "万亿": 1e12,
    "千万": 1e7,
    "百万": 1e6,
    "万": 1e4,
    "千": 1e3,
}
# e2-r1 bridge factors are `万/亿` from the Chinese scale table, shared with prose parsing (review F9c).
_WEAK_SCALE_FACTORS = (_CN_UNITS["万"], _CN_UNITS["亿"])

_EN_UNITS = {
    # thousand
    "k": 1e3,
    "K": 1e3,
    "thousand": 1e3,
    "Thousand": 1e3,
    # million — financial shorthands "MM" / "Mn" / "mn" all = 1e6
    "m": 1e6,
    "M": 1e6,
    "MM": 1e6,
    "mm": 1e6,
    "Mn": 1e6,
    "mn": 1e6,
    "MN": 1e6,
    "million": 1e6,
    "Million": 1e6,
    "mil": 1e6,
    "Mil": 1e6,
    # billion
    "b": 1e9,
    "B": 1e9,
    "bn": 1e9,
    "Bn": 1e9,
    "BN": 1e9,
    "billion": 1e9,
    "Billion": 1e9,
    "bil": 1e9,
    "Bil": 1e9,
    # trillion
    "t": 1e12,
    "T": 1e12,
    "tn": 1e12,
    "Tn": 1e12,
    "TN": 1e12,
    "trillion": 1e12,
    "Trillion": 1e12,
}
_PCT_UNITS = {
    "%": 1e-2,
    "百分点": 1e-2,
    "个百分点": 1e-2,
    "个点": 1e-2,
    "pp": 1e-2,
    "PP": 1e-2,
    "pct": 1e-2,
    "Pct": 1e-2,
    "PCT": 1e-2,
    "percent": 1e-2,
    "Percent": 1e-2,
    "PERCENT": 1e-2,
    "‰": 1e-3,
    "千分点": 1e-3,
    "permille": 1e-3,
    # basis point (finance)
    "bp": 1e-4,
    "bps": 1e-4,
    "BP": 1e-4,
    "BPS": 1e-4,
    "basis_point": 1e-4,
    "basis_points": 1e-4,
}
_CURRENCY_PREFIX = re.compile(
    r"^(?:HK\$)|^[¥$€£₹₩₽￥]"
    r"|^(?:USD|CNY|EUR|GBP|HKD|JPY|KRW|INR|AUD|CAD|RMB|SGD|CHF)\s+",
)
# Thousands-grouping commas: end with `(?!\d)`, **not** `\b`. Python `re` treats both CJK and ASCII letters as
# `\w`, so `\b` fails when a unit immediately follows three digits: `43,964万` has no boundary between `4` and `万`.
# The comma remains, and `float("43,964万")` still has `43,964` after suffix removal -> ValueError
# -> `_parse_value_to_float` returns None -> the number **cannot receive a citation binding**.
# Measured failing forms: `43,964万元` / `1,234亿` / `1,234K` / `1,234M` / `-1,234万`, all
# standard BI-report notation, affecting prose and tables alike.
# `(?!\d)` permits only a nondigit afterward; malformed groups (`1,2345` / `12,34`) remain unrecognized as thousands grouping.
_THOUSAND_SEP = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")

# Pre-built ordered list of (suffix, multiplier) — longest-first so `percent`
# wins over `t`, `million` over `m`, `亿元` over `元`. Built once at import.
_SUFFIX_LONGEST_FIRST: list[tuple[str, float]] = sorted(
    [(k, v) for table in (_CN_UNITS, _EN_UNITS, _PCT_UNITS) for k, v in table.items()],
    key=lambda kv: len(kv[0]),
    reverse=True,
)

# Range marker matcher: capture `lo` and `hi` numbers separated by an inner
# dash (NOT a leading negative). Optional inline scale suffix at end.
# Examples:
#   17.0-17.4%     → lo=17.0 hi=17.4 scale=%
#   8-10           → lo=8 hi=10
#   1.2e9-1.5e9    → scientific endpoints
#   -3.0--2.5      → leading neg + inner range (rare but admit)
_RANGE_INNER_DASH = re.compile(
    r"^(?P<lo>[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"\s*-\s*"
    r"(?P<hi>[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"(?P<scale>[A-Za-z\u4e00-\u9fff%‰]*)$"
)


def _strip_plus_to_float(s: str) -> float:
    """`+2.38` → 2.38; `-1.5` → -1.5; `1.16e9` → 1.16e9. Raises ValueError on malformed."""
    s = s.strip()
    if s.startswith("+"):
        s = s[1:]
    return float(s)


def _unit_scale_multiplier(
    unit: str,
    value_for_ratio_check: float,
    *,
    allow_already_ratio: bool = True,
) -> float | None:
    """Compute multiplier from unit hint (percent / CN / EN scale).

    Returns None if unit doesn't match any scale table — caller leaves
    value unscaled. ``allow_already_ratio`` triggers the percent <1 heuristic.
    """
    u = unit.strip()
    for cur in _UNIT_CURRENCY_TAILS:
        if len(u) > len(cur) and u.endswith(cur):
            u = u[: -len(cur)].strip()
            break
    if not u:
        return None
    if u in _PCT_UNITS:
        if allow_already_ratio and 0 < value_for_ratio_check < 1:
            return 1.0  # already-ratio heuristic
        return _PCT_UNITS[u]
    if u in _CN_UNITS:
        return _CN_UNITS[u]
    if u in _EN_UNITS:
        return _EN_UNITS[u]
    u_lower = u.lower()
    if u_lower != u:
        if u_lower in _PCT_UNITS:
            if allow_already_ratio and 0 < value_for_ratio_check < 1:
                return 1.0
            return _PCT_UNITS[u_lower]
        if u_lower in _EN_UNITS:
            return _EN_UNITS[u_lower]
    return None


# Currency tail tokens we strip from the marker's `:U` field BEFORE scale lookup —
# normalises `万元` -> `万`, `亿美元` -> `亿`, etc. Order matters: longest-first to
# avoid `元` short-circuiting `美元` etc.
_UNIT_CURRENCY_TAILS: tuple[str, ...] = (
    # Multi-char CN currency names
    "人民币",
    "港币",
    "港元",
    "美元",
    "欧元",
    "日元",
    "韩元",
    "卢比",
    "新加坡元",
    "澳元",
    "加元",
    "瑞郎",
    # Single-char tail
    "元",
    "圆",
    "块",
    # ISO codes (cased + lowercase)
    "CNY",
    "USD",
    "EUR",
    "GBP",
    "HKD",
    "JPY",
    "KRW",
    "INR",
    "AUD",
    "CAD",
    "RMB",
    "SGD",
    "CHF",
    "cny",
    "usd",
    "eur",
    "gbp",
    "hkd",
    "jpy",
    "krw",
    "inr",
    "aud",
    "cad",
    "rmb",
    "sgd",
    "chf",
)


def _parse_value_to_float(
    v_str: str,
    unit: str | None = None,
) -> float | tuple[float, float] | None:
    """Parse LLM-emitted value string (post-regex match) to canonical float
    OR a (lo, hi) tuple when the marker is a range like ``17.0-17.4``.

    The optional ``unit`` is the marker's ``:U`` field (e.g. ``percent`` / ``pct``).
    LLM commonly splits the percent sign across value/unit (e.g. ``[num:10.47:percent]``
    rather than ``[num:10.47%:percent]``); without consulting ``unit`` here, those
    markers parse as ``10.47`` and miss the raw ``0.1047`` ratio in the lookup map.

    Sprint ι dogfood-1 expansion (2026-05-09):
    - ``+`` prefix tolerated (LLM uses ``+2.38%`` for growth rates).
    - Scientific notation (``1.16e9``) accepted (LLM uses for large numbers).
    - Range form (``17.0-17.4``) returns ``(lo, hi)`` tuple; caller handles
      via ``_resolve_value``'s range-overlap branch.

    Returns None on:
    - Double-suffix tokens (e.g. '1k万' — strips '万' → 'float("1k")' → ValueError → None).
    - Non-numeric residue ('abc', '1.2.3').
    - Already-numeric strings that fail float() (rare; covers "" / whitespace).

    Preserves Sprint γ rule: '不绑 src 比绑错安全' (don't-bind-on-fail beats wrong-bind).
    """
    if not v_str:
        return None
    s = _CURRENCY_PREFIX.sub("", v_str.strip())
    s = _THOUSAND_SEP.sub("", s)

    # Range detection: look for an internal `-` that separates two numbers
    # (NOT a leading negative). Pattern: optional sign, digits/decimals/sci,
    # then `-`, then another number. Only treat as range when both sides parse.
    rng = _RANGE_INNER_DASH.match(s)
    if rng:
        lo_s, hi_s, scale_suffix = rng.group("lo"), rng.group("hi"), rng.group("scale") or ""
        # Apply suffix scale to BOTH endpoints if present in-line.
        mult = 1.0
        if scale_suffix:
            for suf, m in _SUFFIX_LONGEST_FIRST:
                if scale_suffix == suf:
                    mult = m
                    break
        try:
            lo_f = _strip_plus_to_float(lo_s) * mult
            hi_f = _strip_plus_to_float(hi_s) * mult
        except (ValueError, TypeError):
            return None
        # Apply unit hint scale (percent / CN / EN) to both endpoints.
        if unit and not scale_suffix:
            unit_mult = _unit_scale_multiplier(unit, lo_f, allow_already_ratio=True)
            unit_mult_hi = _unit_scale_multiplier(unit, hi_f, allow_already_ratio=True)
            if unit_mult is not None:
                lo_f *= unit_mult
            if unit_mult_hi is not None:
                hi_f *= unit_mult_hi
        return (min(lo_f, hi_f), max(lo_f, hi_f))

    # Longest-match-first across all suffix tables — prevents single-char EN
    # scales (e.g. `t`/`m`/`b`) from short-circuiting longer compounds like
    # `percent` (`"percent".endswith("t")` is True!).
    for suffix, mult in _SUFFIX_LONGEST_FIRST:
        if s.endswith(suffix):
            try:
                return _strip_plus_to_float(s[: -len(suffix)].strip()) * mult
            except ValueError:
                return None  # double-suffix safe-fail (P1 fix #8)
    try:
        bare = _strip_plus_to_float(s)
    except ValueError:
        return None
    if unit:
        m = _unit_scale_multiplier(unit, bare, allow_already_ratio=True)
        if m is not None:
            return bare * m
    return bare


def _precision_window(v_str: str | None, canonical_v: float) -> float:
    """写出精度 → 容差窗口：**末位的半个单位**。

    含义就一句话：「写到哪一位，就容许那一位的一半」。`53369万` 的末位是 1万，
    窗口 ±5000；`10亿` 的末位是 1亿，窗口 ±5e7；`10.47%` 的末位是 0.01%，窗口 ±5e-5。
    这正是「四舍五入到该精度」的定义 —— 真值落在半个末位以内，写出来就是这个数。

    ★ 为什么换掉原来的「按小数位数分档取 |V| 的百分比」（116 实测逼出来的）：
    档位的键是**小数位数**，而它对整数是**瞎的** —— `10亿`（2 位有效数字）和
    `108350万`（6 位）拿到同一个 ±10%。后果不是判错单个数，而是**分辨不出候选**：
    `_resolve_value` 的 ambiguous-drop 规则是「窗口内 ≥2 个候选就丢弃」，而排名表里
    相邻档位天然在 10% 以内。116 真机实测一张 5 行门店表 —— 三行因为「自己 + 邻居
    都落在 ±10% 里」被判二义丢弃，只有首尾两行（邻居够远）绑上，密度 2/5。
    逐对判断其实没错（13 个用例里旧规则只错 1 个），错的是**窗口宽到失去分辨力**。

    ★ 为什么不用 `analysis_payload.is_faithful_rounding`（核验侧那条）：它是**枚举**
    （1-6 位有效数字 + 0-6 位小数），枚举必然有洞，而洞在绑定侧全是误杀
    （只会少绑，永远不会多绑）。实测反例：`1万` 对真值 6000 —— 按万取整确实是 1万，
    但它不是 6000 的任何一档有效数字舍入，枚举式判 False。本函数是连续公式，没有洞。
    两侧判据不同在这里是**有理由的**：核验宁可放过（避免冤枉），绑定宁可不绑（避免错指）。

    `v_str` 缺失 / 取不出数字时回退 ±2%（保持原兜底）。
    """
    if not v_str:
        return abs(canonical_v) * 0.02
    # Extract the written numeric portion (`108,350万` -> `108,350`); following scales/units do not affect precision calculation.
    # ⚠ Strip currency prefixes first: `_AUTOWRAP_PLAIN_RAW` explicitly accepts forms such as `¥1,234` / `HK$1.2亿`,
    # but the numeric scan below stops at the first nonnumeric character. Without stripping, every `¥…` yields no number and falls back to ±2%,
    # denying precision windows to currency-prefixed numbers (self-check: `¥1,234` had a 2% window versus `1,234` at 0.04%).
    s = _CURRENCY_PREFIX.sub("", v_str.strip()).lstrip("+-").lstrip()
    digits = ""
    for ch in s:
        if ch.isdigit() or ch in ".,":
            digits += ch
        else:
            break
    digits = digits.replace(",", "").rstrip(".")
    try:
        written = abs(float(digits))
    except ValueError:
        return abs(canonical_v) * 0.02
    if written == 0:
        return abs(canonical_v) * 0.02
    dot = digits.find(".")
    decimals = 0 if dot < 0 else len(digits) - dot - 1
    # canonical_v = written number × scale (ten thousand/hundred million/% …). The written-unit last digit is 10^-decimals;
    # multiplying by scale gives its canonical size, and half that is the rounding radius.
    scale = abs(canonical_v) / written
    return 0.5 * (10.0**-decimals) * scale


def _resolve_value(
    canonical_v: float | tuple[float, float],
    value_to_source: dict[float, str],
    *,
    raw_value_str: str | None = None,
    strong_keys: frozenset[float] | None = None,
) -> str | None:
    """Sprint ι (PRD §10.1.1.4 + dogfood-1 range + dogfood-1 D):
    precision-aware tolerance OR range-overlap lookup with ambiguous-key invariant.

    Single value (``float``):
    - Exact key in map → bind.
    - 1 unique near-match within precision window → bind. Window adapts
      to LLM-emit precision (see ``_precision_window``).
    - 0 OR ≥2 near-matches → drop (don't bind wrong src).

    Range value (``(lo, hi)`` tuple, e.g. ``17.0-17.4%`` for forecast/estimate):
    - 1 unique key inside [lo, hi] → bind to that source (likely sandbox call).
    - 0 OR ≥2 keys inside → drop.
    """

    # Review F2: prioritize strong values within the window. If any strong value occurs there, including ambiguity drops absent from the map,
    # limit candidates to strong∩map; weak keys cannot replace ambiguous values or take over a strong-value window.
    # strong_keys=None (legacy callers/maps without weak sources) preserves previous behavior.
    def _pick(cands: list[float]) -> str | None:
        if len(cands) == 1:
            return value_to_source[cands[0]]
        return None

    if isinstance(canonical_v, tuple):
        lo, hi = canonical_v
        if strong_keys:
            strong_in = [k for k in strong_keys if lo <= k <= hi]
            if strong_in:
                return _pick([k for k in strong_in if k in value_to_source])
        return _pick([k for k in value_to_source if lo <= k <= hi])
    if canonical_v in value_to_source:
        return value_to_source[canonical_v]
    window = _precision_window(raw_value_str, canonical_v)
    if strong_keys:
        strong_in = [k for k in strong_keys if abs(k - canonical_v) <= window]
        if strong_in:
            return _pick([k for k in strong_in if k in value_to_source])
    return _pick([k for k in value_to_source if abs(k - canonical_v) <= window])


# Sprint ι dogfood-1 (B option): high-confidence plain-number patterns we
# auto-wrap when LLM emits prose like "13.9亿元" / "1,921,861" / "17.8%"
# without using marker syntax. Conservative: require explicit unit /
# currency prefix / multi-comma thousand separator so bare digits like
# "Top 5" or order numbers stay un-wrapped.
_AUTOWRAP_PLAIN_RAW = re.compile(
    # Review F3: `(?<!\d,)` prevents restarting a match after a thousands-grouping comma. When the tail guard rejects
    # the ¥ branch of `¥1,588.2M`, the scanner otherwise rematches `588.2M` after the comma as a separate number,
    # inserting the citation inside the number. Both lookbehinds are fixed-width and valid together.
    r"(?<![:.\dA-Za-z_])(?<!\d,)"  # not mid marker / decimal / ASCII word / thousand-sep
    r"("
    r"[¥$€£]\s?\d[\d,]*(?:\.\d+)?"  # currency-prefixed
    r"|"
    # ★ Permit whitespace between numbers and units: verification's `_MARK_NUM_RE` has always used `\s*`, but rejecting it here
    # makes `108,350 万元` / `2.44 亿元` visible to verification but invisible to binding, adding only to the unverified denominator
    # rather than the verified numerator. Writers instructed to put units in tables produced precisely these spaced forms.
    # Use `[^\S\n]*` instead of `\s*`: both sides currently run **line by line** (binder via `_bind_line`),
    # so `\s` cannot consume newlines there, but the regex itself can span lines (self-check `"排名 5\n万元级门店"` -> `5\n万`),
    # silently misbinding if later used on whole paragraphs. Exclude newlines with no behavior change today.
    r"[+-]?\d[\d,]*(?:\.\d+)?[^\S\n]*(?:亿|万亿|千万|百万|万|千|%|‰)"  # CN scale or percent
    r"|"
    r"[+-]?\d+(?:\.\d+)?(?:[KMBT](?![A-Za-z]))"  # EN single-letter scale
    r"|"
    r"[+-]?\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?"  # multi-comma thousand-sep (≥1,000,000)
    r"|"
    # Source-accessibility presentation e2: **bare-yuan forms** (`60,484元` / `351.4元` / `578元`).
    # The main currency notation in Chinese BI reports previously could not bind: yuan is absent from the scale table, and ¥ prefixes
    # rarely appear in Chinese prose (116 measurement: class D, numbers without bindings, was 31% of paragraphs; yuan forms were the largest cluster).
    # Yuan is a strong unit signal like ¥ (Top 5 / third do not contain yuan), preserving conservatism.
    # ⚠ Use yuan in a **lookahead outside the capture group**: `_parse_value_to_float` parses only bare/scaled numbers
    # (`60,484元`->None, `60,484`->60484.0). Capturing yuan would always yield None and prevent binding;
    # `_UNIT_SWALLOW_RE` still places `[N]` after units (its table starts with `元/亿元/万元`).
    # `108,350 万元` still matches the scale branch first (alternative order at the same start, `万` first);
    # precedence among overlapping families determines correctness. `美元` does not match (lookahead starts with `美`).
    r"[+-]?\d[\d,]*(?:\.\d+)?(?=[^\S\n]*元)"
    r")"
    # e2-r1 dry run found `¥1,588.2M` backtracking to the partial match `¥1` (excluding trailing M makes it retreat digit by digit
    # until before the comma), rendering the citation inside the number as `¥1 [1],588.2M`. Add `,\d` exclusion:
    # matches cannot end before a thousands-group continuation; prefer leaving this whole form unbound.
    r"(?![A-Za-z0-9_%‰.]|,\d)"  # not followed by ASCII word/digit/decimal/thousand-sep continuation
    # Feishu 07.06 #11: a numeric value immediately followed by [N] is already explicitly cited by the writer (registry
    # owns integer numbering). Wrapping again in [num:…:src:…] stacks two marker generations on one number
    # (prod: `[num:4.18%:src:call_…][3]`), causing double frontend decoration and double trust counting.
    # Skip it: frontend citation-vnum (a number adjacent to [N]) already renders the number as clickable and trusted.
    r"(?!\s*\[\d{1,3}\])"
)


# Query keywords that trigger genuine grouped Datarc charts: any match means the ask_question deliberately
# requests a grouped chart, making its image URL worth embedding. Images without these words are usually Datarc
# fallback for unrecognized queries (input box + generic discover page), so embedding them misleads.
_CHART_HINT_WORDS = (
    "按",
    "各",  # Grouping terms: "按门店" / "各门店".
    "每月",
    "每日",
    "每周",
    "每季",
    "每年",
    "每家",
    "每个",
    "每项",
    "每条",  # "每家店" / "每个品类"
    "趋势",
    "走势",
    "走向",
    "分布",
    "占比",
    "对比",
    "top",
    "TOP",
    "前",
    "后",
    "排名",
    "哪些",
    "哪几",
    "全部",
    "所有",  # "哪些门店" / "所有品类"
)


_COL_HEADERS = {
    "query": "查询",
    "metric": "指标",
    "content": "数值",
    "value": "原值",
    "dimension": "维度",
    "attr": "属性",
    "previous": "去期",
    "current": "本期",
    "delta": "变化",
}
_NUMERIC_COLS = {"value", "previous", "current", "delta"}


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_HEAD_RE = re.compile(r"^\|.+\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\|\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|\s*$")
_BLOCKQUOTE_RE = re.compile(r"^>\s*(.*)$")
_OL_RE = re.compile(r"^\s*(\d+)[.、)]\s+(.+)$")
_UL_RE = re.compile(r"^\s*[-*•]\s+(.+)$")
_KV_RE = re.compile(r"^\s*[-*•]?\s*\*\*([^*]+)\*\*\s*[：:]\s*(.+?)\s*$")
_EMOJI_BLOCK_RE = re.compile(r"^\s*[⚠🔔💡✅❌📊🔍📉📈🎯⭐]\s*(.+)$")
_NEXT_ACTION_WORDS = (
    "立即",
    "启动",
    "推行",
    "上线",
    "试点",
    "复盘",
    "复查",
    "推广",
    "建议",
    "下一步",
    "优先",
    "锁定",
    "执行",
    "搭建",
    "跟进",
)


# Table separator (`|---|---|`, optionally with alignment colons).
_TABLE_SEP_INLINE_RE = re.compile(r"\|(?:\s*:?-{2,}:?\s*\|){1,}")

# Chart reference placeholders (#92 interleaved charts).
# Two forms:
# 1. Explicit: `<!-- chart:sec_xxxxxxxxxxxx -->` (intended Lead syntax).
# 2. Bare: `sec_xxxxxxxxxxxx` on its own line (fallback for a Lead leak missing the comment wrapper).
_CHART_REF_COMMENT_RE = re.compile(
    r"<!--\s*chart:(sec_[a-fA-F0-9]{8,})\s*-->",
    re.IGNORECASE,
)
_BARE_SEC_ID_RE = re.compile(
    r"^\s*(sec_[a-fA-F0-9]{8,})\s*$",
    re.MULTILINE,
)


def _unverified_numbers_in_text(
    text: str,
    authorized: set[float],
    *,
    trace: str = "",
) -> list[str]:
    """扫 text 里所有数字，返回**未授权**（不在 authorized 容差内）的 raw 文本列表.

    复用 number_verifier 的 verify_numbers_in_text（含单位归一化 + 派生识别 + 年份/小数白名单）.
    前端靠这个字段给 narrative section 画警告徽章.
    """
    if not text or not authorized:
        return []
    try:
        from bowhead_core.number_verifier import verify_numbers_in_text
    except ImportError:
        return []
    # rel_tol was removed with number_verifier's switch to faithful rounding criteria: its old blanket 0.03 window
    # both accepted genuine 1.9% fabrication and rejected valid zero-decimal rounding.
    unauth = verify_numbers_in_text(text, authorized, trace=trace)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for raw, _ in unauth:
        if raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────
_TIME_HINT_RE = re.compile(r"(近\s*\d+\s*(天|日|周|月|年)|今年|去年|本月|上月|\d{4}\s*年(\s*\d{1,2}\s*月)?|Q[1-4]|YTD|MTD)")

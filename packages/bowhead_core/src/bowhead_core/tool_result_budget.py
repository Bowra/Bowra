"""Sprint ψ-B — per-tool result token cap.

Drain-side cap: shrinks `inv.result` that enters LLM context (messages[]),
preserving the full original via `inv.result_full` snapshot for SSE / drawer
consumers.

P0-4 fix (CTO + Eng): cap mutation must NOT poison SSE drawer. Caller
snapshots `inv.result_full = inv.result` BEFORE calling `cap_result()`, then
sends `result_full` to SSE inline path and `result` (capped) into messages.

Invariants per PRD §3.2:
- artifact_id preserved
- columns preserved (for bi_query)
- total_rows preserved
- cap never raises (try/except returns original)
- cap is idempotent
"""

from __future__ import annotations

from typing import Any

from bowhead_skill.sandbox_call_shape import sandbox_inputs_hint

from bowhead_core.model_view import project_tool_result
from bowhead_core.token_estimator import count_tokens_of_obj

# Per-tool token caps (PRD §3.2 r2)
DEFAULT_CAPS_TOKEN: dict[str, int] = {
    "bi_query": 12500,
    "bi_fetch_rows": 50,  # always artifact-only
}


def cap_result(tool_name: str, result: Any) -> tuple[Any, int]:
    """Cap result token size per tool.

    Returns (possibly-shrunk result, tokens_saved).

    Some caps are unconditional (e.g. bi_fetch_rows is always artifact-only). Others
    only fire when over the per-tool token budget (e.g. bi_query rows).

    On error, returns original (never raise).
    """
    if not isinstance(result, dict):
        return result, 0
    if tool_name not in DEFAULT_CAPS_TOKEN:
        return result, 0
    cap_tok = DEFAULT_CAPS_TOKEN[tool_name]

    try:
        original_tok = count_tokens_of_obj(result)
        if tool_name == "bi_query":
            # Token-gated: only shrink if over cap
            if _model_tokens(result) <= cap_tok:
                return result, 0
            new_r = _cap_bi_query(result, cap_tok)
        elif tool_name == "bi_fetch_rows":
            # Unconditional: always artifact-only
            new_r = _cap_fetch_rows(result)
        else:
            return result, 0

        new_tok = count_tokens_of_obj(new_r)
        return new_r, max(0, original_tok - new_tok)
    except Exception:
        return result, 0


def _model_tokens(r: Any) -> int:
    """Tokens of the bi_query copy the model actually receives.

    `model_view` strips backend-only keys (`_debug_cut_props`, `_debug_explain_message`, filenames) before the
    result reaches the model, so they must not spend its budget. Measuring the raw result let those keys push a
    nine-row result over the cap and empty its rows, although the model would never have seen them.
    """
    return count_tokens_of_obj(project_tool_result("bi_query", r))


def _halve_rows_to_fit(d: dict, key: str, cap_tok: int, full: Any) -> None:
    """Halve the row window until the model's copy fits, keeping at least one row.

    Dropping every row is the last resort: with zero rows the model has to reach for the artifact or answer from
    nothing, while a few real rows plus the truncation note still ground the answer. The note is rewritten with
    each cut so it never claims more rows than are present.
    """
    rows = list(d.get(key) or [])
    while len(rows) > 1 and _model_tokens(d) > cap_tok:
        rows = rows[: len(rows) // 2]
        d[key] = rows
        d["_rows_truncated"] = True
        _sync_rows_shown(d, len(rows))
        d["_preflight_hint"] = _replace_truncation_note(d.get("_preflight_hint"), len(rows), full)


def _cap_bi_query(r: dict, cap_tok: int) -> dict:
    """Dispatch: batch shape `{results: List}` vs single (legacy flat shape).

    Sprint χ-3 + r3-review CTO-1 P0: batch result must NOT scale total_budget
    by N (would撕 ψ red line). Total封顶 = cap_tok × 1.0 hard cap;
    per-sub cap = min(400 tok, cap_tok // N).
    """
    if isinstance(r.get("results"), list):
        return _cap_bi_query_batch(r, cap_tok)
    return _cap_bi_query_single(r, cap_tok)


def _shorten_sandbox_hint(full: str, artifact_id: Any) -> str:
    """把完整的沙箱 how-to 降级成短版；渲染仍走 `sandbox_call_shape` 那一份。

    拿不到 artifact_id 时保留原句 —— 宁可占点预算，也不要给一句没有 id 的骨架。
    """
    if not isinstance(artifact_id, str) or not artifact_id:
        return full
    # Entry is already unknown here (trimming happens outside handler); use fallback wording valid for both paths.
    return sandbox_inputs_hint(None, artifact_id=artifact_id, short=True)


#: Stable truncation-note marker: trimming **replaces** the old handler sentence using it (row count changes).
#: Preserve this substring when changing wording, or old note stays and model receives two conflicting counts.
TRUNCATION_NOTE_MARK = "只给你看了"


def rows_truncation_note(shown: int, full: Any) -> str:
    """「只给你看了一部分」那句提醒——bi_query handler 与裁剪层共用这一份措辞。

    ★ 末尾那句「再查一次结果一样」是**防重试循环**用的，不是废话：
    `row_count` 改成全量（`19fee1bbe`，2026-09-15）之后，模型看到
    `rows_shown=10 / row_count=358` 会合理地认为「数据没取全，再要一次」。
    116 实测 `t_a091c1b59a78`：同一参数的 `bi_query` **连发 21 次、其中 16 次逐字节相同**，
    每次各建一个 artifact，51 秒全耗在这上面。它缺的不是数据，是「这是展示上限」这句话。
    """
    return (
        f"⚠ 只给你看了 {shown} 行，全量 {full} 行在 artifact_id 里。"
        f"**不要**当作只有 {shown} 行做计数、统计或下结论；需要全量走 python_sandbox + artifact_id。"
        "这是**展示**上限（为了不撑爆上下文），调大 `inline_max_rows` 或重查同一条都不会给到更多行。"
    )


def unsorted_window_note(metric: str) -> str:
    """这 N 行不是按指标排序的 —— 别据此判断谁最大。

    116 dogfood（1.3.46 真机）A/B，同一天同一个库：

      · `t_669060c7504f` 38 行按 `营业额` **降序** ⇒ 内联 10 行里就有真前三 ⇒ 答案全对。
      · `t_f4edf5b877b3` 390 行**没排序**（内联值 0.0, 0.009, 0.499, 0.0 …）⇒ 模型把窗口里
        最大的内暖衣(4988万)、厨房用具(773万)写成「绝对的销售支柱」，而真第一名
        女装(62.49 亿，大一个数量级)整段正文一次没提。

    差别只有一个：**窗口有没有按指标排序**。「不要据此下结论」那句当时就在返回里，
    模型照做了它明令禁止的事——所以这里补的是**事实**（窗口是什么顺序），不是又一句劝告。
    刻意不编一份「top 5」：390 行是「月 × 品类」，按单元格取前 5 会得到
    「1月的女装」这种**不是品类合计**的东西，比不说更坏。
    """
    return (
        f"⚠ 这几行**不是按 `{metric}` 排序**的（是全量里的前几行，原始顺序）。"
        "谁最大 / 头部是谁 / Top N —— 一概不能照这几行判断；"
        "要排名请改查询加排序，或用 artifact_id 走 python_sandbox 自己算。"
    )


def _primary_metric_key(rows: Any) -> str | None:
    """内联行里那个「主指标」列名：最后一个数值列（与 `_figure_data_summary` 同一取法）。"""
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    nums = [k for k, v in rows[0].items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return nums[-1] if nums else None


def window_is_metric_sorted(rows: Any, metric: str | None) -> bool:
    """窗口是不是按该指标降序。

    只判**给出来的这几行**：它们若是全量的前 N 行且降序，全量大概率也是降序；
    不降序则全量**一定**不是按它排的 —— 后者才是要示警的那一半，结论方向是安全的。
    """
    if not metric or not isinstance(rows, list) or len(rows) < 2:
        return True
    vals = [r.get(metric) for r in rows if isinstance(r, dict)]
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(nums) < 2:
        return True
    return all(a >= b for a, b in zip(nums, nums[1:]))


def _replace_truncation_note(hint: Any, shown: int, full: Any) -> str:
    """把 `_preflight_hint` 里那句陈旧的截断提醒换成按**裁后**行数重写的一句，其余句子原样留着。

    ⚠ handler 在**裁剪之前**就写下了这句（那时的行数是 30 或 358），裁剪层随后把行砍到 10
    却不改它 ⇒ 提示说 30、手里只有 10。而 handler 认为「没截断」时（`inline_max_rows` 调得够大）
    压根没写这句 ⇒ 裁剪层砍完之后**一句提示都没有**——模型要得越多、被告知得越少，
    于是同参数重试到死（116 实测 16 次）。两种情形都由这里兜住。
    """
    parts = [p.strip() for chunk in str(hint or "").split("\n") for p in chunk.split("; ")]
    kept = [p for p in parts if p and TRUNCATION_NOTE_MARK not in p]
    kept.append(rows_truncation_note(shown, full))
    return "\n".join(kept)


def query_limit_note() -> str:
    """datarc 按请求截断了结果（G §2.6）——bi_query handler 与裁剪层共用这一份措辞。

    ⚠ 与 `rows_truncation_note` 是两回事：那句是「我们只给你看了一部分，全量在 artifact 里」；
    这句是「数据本身就只有这些」。措辞写混了，模型会去 artifact 里找根本不存在的其余行。
    """
    return (
        "⚠ datarc 按请求里的条数限制截断了结果：这批行本身就只有这些，不是全集，row_count 是截断后的行数。要回答「一共有多少个」须去掉条数限制重查。"
    )


def _notes_to_keep(r: dict, shown: int, full: Any) -> str:
    """超预算时 `_preflight_hint` 里必须留下的几句：截断、条数限制、没认出的词、没筛过的事实。

    按字段重新生成，不解析提示字符串。这几句不是「数据越大越需要」就是「漏了就是数字错」
    （bi-answer-correctness C / A / G），恰恰不能在裁剪时丢。
    """
    from bowhead_core.bi_unknown_words import engine_applied_no_filter, unfiltered_fact_note, unknown_words_note

    notes: list[str] = []
    if r.get("_rows_truncated"):
        notes.append(rows_truncation_note(shown, full))
    if r.get("_rows_limited_by_query"):
        notes.append(query_limit_note())
    parse = r.get("_parse")
    words = parse.get("unknown_words") if isinstance(parse, dict) else None
    if words:
        notes.append(unknown_words_note(words))
        if engine_applied_no_filter(r.get("query_explain")):
            notes.append(unfiltered_fact_note(words))
    return "\n".join(notes)


def _sync_rows_shown(d: dict, shown: int) -> None:
    """裁剪改了行数就同步 `rows_shown` 与截断标记。`row_count` 是全量，永远不改。"""
    if "rows_shown" not in d and "row_count" not in d:
        return
    d["rows_shown"] = shown
    full = d.get("row_count")
    if isinstance(full, int) and shown < full:
        d["_rows_truncated"] = True


def _cap_bi_query_single(r: dict, cap_tok: int) -> dict:
    """Legacy single-query cap (0.14.1 behavior).

    **Shorten** the sandbox how-to + trim rows to 10. If still > cap, shorten the
    preflight hint; if still > cap, halve the rows; drop them entirely only when
    even one row does not fit. Budget is measured on the model's projection.

    ⚠ sandbox-input-contract L3：`_how_to_use_in_sandbox` 原先是**第一个被 pop 掉的**。
    数据越大越需要进沙箱算，恰恰在最需要它的时候删掉，只留下面那句没有形状的
    `Use python_sandbox + INPUTS to load.`（没说 inputs 写在哪，也没说代码里怎么取）
    ——而「不知道 inputs 写哪」正是 116 三十天里输入寻址类失败的主因。
    现在降级成短版（约 40 token）而不是删除；短版仍带骨架 + 取法两件套。

    ⚠ bi-answer-correctness C（2026-09-15）：`_preflight_hint` 原先也是**第一步就整条删掉**，
    而截断提醒就在里面——数据越大越需要「只给你看了一部分」这句，恰恰在最需要时丢了
    （116 截断后只有 19.8% 去读了全量）。现在先裁行；仍超预算才把提示换成只剩截断那一句，
    没截断的才整条删。`row_count`（全量）不动，`rows_shown` 随裁剪同步。
    """
    r = dict(r)
    _howto = r.get("_how_to_use_in_sandbox")
    if _howto:
        r["_how_to_use_in_sandbox"] = _shorten_sandbox_hint(str(_howto), r.get("artifact_id"))

    rows = r.get("rows") or []
    full_count = r.get("_rows_full_count") or r.get("row_count") or r.get("total_rows") or len(rows)
    if len(rows) > 10:
        r["rows"] = rows[:10]
        r["_rows_truncated"] = True
        r["_rows_inline_count"] = 10
        r["_rows_full_count"] = full_count
        _sync_rows_shown(r, 10)
        # ★ Whenever rows are actually trimmed, **unconditionally** rewrite the note. Previously regenerated only
        # when still over budget after trimming (`_notes_to_keep`), but 10 rows usually fit ⇒ branch never reached.
        r["_preflight_hint"] = _replace_truncation_note(r.get("_preflight_hint"), 10, full_count)
        # If window is not metric-sorted, add the **fact** that these rows cannot determine the leader.
        # See `unsorted_window_note`: same day/database, sorting determines answer correctness.
        _metric = _primary_metric_key(r.get("rows"))
        if _metric and not window_is_metric_sorted(r.get("rows"), _metric):
            r["_preflight_hint"] = f"{r['_preflight_hint']}\n{unsorted_window_note(_metric)}"

    if _model_tokens(r) > cap_tok and r.get("_preflight_hint"):
        # Regenerate and retain truncation sentence (C) and unrecognized-token sentence (A) by fields; discard only other explanations.
        kept = _notes_to_keep(r, len(r.get("rows") or []), full_count)
        if kept:
            r["_preflight_hint"] = kept
        else:
            r.pop("_preflight_hint", None)

    if _model_tokens(r) > cap_tok and "artifact_id" in r:
        _halve_rows_to_fit(r, "rows", cap_tok, full_count)
        if r.get("_rows_truncated"):
            r["_rows_inline_count"] = len(r.get("rows") or [])
            r["_rows_full_count"] = full_count

    # If even one row does not fit, drop rows entirely — artifact_id is the safety net
    if _model_tokens(r) > cap_tok and "artifact_id" in r:
        r["rows"] = []
        r["_rows_inline_count"] = 0
        _sync_rows_shown(r, 0)
        if r.get("_preflight_hint"):
            r["_preflight_hint"] = _notes_to_keep(r, 0, full_count) or r["_preflight_hint"]
        r["_rows_dropped"] = f"see artifact_id ({full_count} rows full; inline-capped at 0 to stay under context budget). " + sandbox_inputs_hint(
            None, artifact_id=str(r.get("artifact_id") or "art_..."), short=True
        )
    return r


def _cap_bi_query_batch(r: dict, cap_tok: int) -> dict:
    """χ-3 batch result cap. Total budget HARD封顶 cap_tok × 1.0 (CTO-1 P0).
    Per-sub cap = max(400, cap_tok // N). Merge & dedupe preflight_warnings
    into top-level `_batch_preflight_summary` (≤6 items, ≤120 char each)
    so LLM still sees entity-match warnings without per-sub inline bloat.
    """
    r = dict(r)
    results = list(r.get("results") or [])
    n = len(results)
    if n == 0:
        return r

    per_cap = max(400, cap_tok // max(n, 1))
    total_budget = cap_tok  # HARD cap (no scale-by-N — defends ψ red line)

    # Merge + dedupe preflight_warnings across subs into top-level summary.
    all_warnings: list[str] = []
    seen: set[str] = set()
    for sub in results:
        if not isinstance(sub, dict):
            continue
        for w in sub.get("_preflight_warnings") or []:
            w_norm = str(w).strip()[:120]
            if w_norm and w_norm not in seen:
                seen.add(w_norm)
                all_warnings.append(w_norm)
    if all_warnings:
        r["_batch_preflight_summary"] = all_warnings[:6]

    # Cap each sub-result.
    capped: list[dict] = []
    cum_tok = 0
    for sub in results:
        if not isinstance(sub, dict):
            capped.append(sub)
            continue
        sub = dict(sub)
        sub.pop("_preflight_warnings", None)  # Already merged into top-level _batch_preflight_summary
        rows = sub.get("sample_rows") or []
        if len(rows) > 5:
            sub["sample_rows"] = rows[:5]
            sub["_sample_truncated"] = True
            _sync_rows_shown(sub, 5)
        # ★★ Dropping rows is the **last step**, not first. Single-query `_cap_bi_query_single` already follows
        # shrink sandbox guidance → trim rows → shrink hints → drop rows only if still excessive. This branch jumped directly
        # to dropping: **measured the whole subresult (query_explain / _parse / lengthy _preflight_hint included),
        # but dropped only rows**, retaining all over-budget metadata and losing the only useful data.
        #
        # 116 `t_664e64068487` (1.3.56): three queries together; `2026年各省营业额` had just **5 rows**,
        # (Shanghai 37.71 亿 / Beijing 36.64 亿 / …), yet exceeded 500 tokens and was emptied.
        # compute peer therefore **never saw any rows**; server artifact-based chart was correct, but body could only fabricate:
        # provinces became `A省 / B省 / C省`; leader claimed `0.71 亿元、占 48.6%、独占鳌头`
        # (truth: 37.71 亿 / 31.3%, runner-up 30.4%, almost tied), and invented `食品类`, absent from data.
        sub_tok = _model_tokens(sub)
        if sub_tok > per_cap:
            _sub_howto = sub.get("_how_to_use_in_sandbox")
            if _sub_howto:
                sub["_how_to_use_in_sandbox"] = _shorten_sandbox_hint(str(_sub_howto), sub.get("artifact_id"))
            sub_tok = _model_tokens(sub)
        if sub_tok > per_cap and sub.get("_preflight_hint"):
            _kept = _notes_to_keep(sub, len(sub.get("sample_rows") or []), sub.get("row_count") or sub.get("total_rows"))
            if _kept:
                sub["_preflight_hint"] = _kept
            else:
                sub.pop("_preflight_hint", None)
            sub_tok = _model_tokens(sub)
        if sub_tok > per_cap:
            _halve_rows_to_fit(sub, "sample_rows", per_cap, sub.get("row_count") or sub.get("total_rows") or len(rows))
            sub_tok = _model_tokens(sub)
        if sub_tok > per_cap:
            _full = sub.get("row_count") or sub.get("total_rows") or len(rows)
            sub["sample_rows"] = []
            _sync_rows_shown(sub, 0)
            # ⚠ After dropping, update the hint truthfully. Previously subquery 2 still claimed 30 rows shown,
            # while `rows_shown` was 0: **telling the model it has 30 rows when it has none**, ideal for fabrication.
            # I already fixed the same omission in `_cap_bi_query_single` (`_replace_truncation_note`),
            # but missed this branch ([[feedback_one_concept_many_implementations]]).
            _kept0 = _notes_to_keep(sub, 0, _full)
            if _kept0:
                sub["_preflight_hint"] = _kept0
            elif sub.get("_preflight_hint"):
                sub.pop("_preflight_hint", None)
            sub["_rows_dropped"] = f"per-query cap ({per_cap} tok); use _ref_artifact_id with python_sandbox"
        cum_tok += _model_tokens(sub)
        capped.append(sub)
        if cum_tok > total_budget:
            # Drop sample_rows for remaining subs but keep metadata.
            for rem in results[len(capped) :]:
                rem_d = dict(rem) if isinstance(rem, dict) else {}
                rem_d.pop("_preflight_warnings", None)
                rem_d["sample_rows"] = []
                _sync_rows_shown(rem_d, 0)
                rem_d["_rows_dropped"] = "total batch budget exhausted; use _ref_artifact_id"
                capped.append(rem_d)
            break
    r["results"] = capped
    return r


def _cap_fetch_rows(r: dict) -> dict:
    """fetch_rows is always artifact-only (rows table can be huge)."""
    r = dict(r)
    if "rows" in r and "artifact_id" in r:
        full_count = r.get("_rows_full_count") or r.get("total_rows") or len(r.get("rows") or [])
        r["rows"] = []
        r["_rows_dropped"] = f"fetch_rows is always artifact-only ({full_count} rows in artifact_id). Use python_sandbox + INPUTS to read."
    return r

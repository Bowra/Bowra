"""datarc `_parse.unknown_words` 的去噪与点名提示（bi-answer-correctness A，2026-09-15）。

116 全量空跑：原样的 `unknown_words` 在 95% 的结果里非空——大多是 compute 自己在词间加的
空格、「各」「按」这类单字。规则天天响，模型早就不看，真漏识别的「华东地」混在里面一起被忽略。

这里只做机械清理：按空白切分、去首尾标点、丢长度 < 2 的片段。**不用正则、不用词表**——
片段是地名、名称还是「汇总」这类操作词，由模型判断（见 compute.md）。
方案：docs/sprints/bi-answer-correctness-2026-09-15/a-unknown-words-signal.md
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable


def _strip_edge_punctuation(piece: str) -> str:
    start, end = 0, len(piece)
    while start < end and unicodedata.category(piece[start]).startswith("P"):
        start += 1
    while end > start and unicodedata.category(piece[end - 1]).startswith("P"):
        end -= 1
    return piece[start:end]


def denoise_unknown_words(words: Iterable[object]) -> list[str]:
    """去噪后的「真没认出的词」，去重保序。

    先切分再判断：datarc 会把相邻的未识别片段连同空格并成一个 token（`IPT 客单`）。
    代价：单字的真漏识别（如「省」）会被丢，116 语料里只出现过 2 次。
    """
    out: list[str] = []
    for word in words:
        for piece in str(word).split():  # No-argument split handles full-width spaces.
            piece = _strip_edge_punctuation(piece)
            if len(piece) >= 2 and piece not in out:
                out.append(piece)
    return out


def engine_applied_no_filter(query_explain: object) -> bool:
    """Return whether the structured report omits its business-filter field."""
    return isinstance(query_explain, dict) and bool(query_explain) and "filters" not in query_explain


def unfiltered_fact_note(words: Iterable[str]) -> str:
    """Describe a missing business-filter field without inferring execution behavior."""
    return "Datarc's structured query report contains no `filters` field."


def unknown_words_note(words: Iterable[str]) -> str:
    """Name parser-reported unknown words; interpretation belongs to the caller."""
    quoted = ", ".join(f"「{word}」" for word in words)
    return (
        f"Datarc's parser reported unrecognized words: {quoted}. "
        "This signal alone does not establish whether those words were ignored or used as literal conditions. "
        "Review the engine-reported query and returned values to decide whether to rephrase the query."
    )


# ── Recognized filter value + no reported condition ⇒ name the values ───────────────────────────────
#
# Customer session, 2026-11-10: "中文通用名=菲诺利单抗 每月 销售金额" parsed cleanly (dimension, `$eq`, value, measure;
# no unknown words), yet the structured report listed only models/measures/dimensions/date_filters and the rows were
# the unfiltered monthly totals. The same words with a measure of another model reported `filters` and returned the
# filtered rows. The missing-`filters` check used to run only when unknown words existed, so nothing was said.
#
# Criteria use key presence only: a parsed value token and a structured report carrying neither `filters` nor `having`
# (`having` is excluded so a threshold value of a measure condition is not reported as a dropped filter).
# The tool states the fact and keeps the rows: whether a value belongs to the requested scope is the model's call,
# since the word may be incidental. Regression check (2026-11-10, 1.7.32): an earlier imperative wording was ignored
# for 「女装 2026年各月 月坪效」, so compute.md and the Datarc protocol now spell out how to judge and act on it.
# 1.7.38 re-dogfood (2026-11-15): two models still ignored it, so `bi_filter_scope` now refuses a compute return
# that records no judgment for these values; the note announces that requirement up front.

_FILTER_VALUE_KINDS = ("value", "dimension_value")


def recognized_filter_values(tokens: object) -> list[str]:
    """Return the filter values in `_parse.tokens` (backend-only), de-duplicated in order."""
    out: list[str] = []
    for token in tokens if isinstance(tokens, list) else []:
        if not isinstance(token, dict) or token.get("kind") not in _FILTER_VALUE_KINDS:
            continue
        value = str(token.get("value") or token.get("word") or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def engine_reported_no_condition(query_explain: object) -> bool:
    """Return whether the structured report carries neither a business filter nor a measure condition."""
    return isinstance(query_explain, dict) and bool(query_explain) and "filters" not in query_explain and "having" not in query_explain


def unapplied_filter_values(parse: object, query_explain: object) -> list[str]:
    """Return the parsed filter values of one result whose structured report lists no condition at all."""
    if not isinstance(parse, dict) or not engine_reported_no_condition(query_explain):
        return []
    return recognized_filter_values(parse.get("tokens"))


def dropped_filter_note(values: Iterable[str]) -> str:
    """Name parsed filter values that the engine-reported query does not list as a condition."""
    quoted = ", ".join(f"「{value}」" for value in values)
    return (
        f"Your query text names these filter values, but the executed query (`query_explain`) applies no `filters` or `having` "
        f"condition, so the returned rows are not restricted to them: {quoted}. "
        "Judge from the request whether each value is part of the scope the user asked for. "
        "If it is, these rows are not that scope's result: requery with a measure or field the schema offers for it, "
        "or tell the user the data cannot be restricted that way, and never label these rows with the value. "
        "If the value is incidental wording, use the rows as returned. "
        "When you finish through subagent_return, record this judgment for each value in `filter_scope_checks`."
    )


# ── Parsed measure or dimension that the structured report does not list ⇒ name it ──────────────────
#
# Regression check on 1.7.42 (2026-11-15), the original regression: after the dropped-filter note, compute requeried
# 「2026年 女装 月坪效 按种类大类 每月」. The parse bound 月坪效 as a measure, but Datarc answered from 门店品牌品类表 and its
# report listed the measure 营业额: the rows were 女装 revenue. Nothing said so; compute took that query as the filtered
# 月坪效 and delivered the all-store 月坪效 rows as 女装. In t_c7554f8fa14f 「2026年 女装 门店面积 每月」 bound 门店面积 as a
# dimension, the report grouped by 每月 only and returned 营业额, and compute appended 求和/总计/汇总/合计 over 13 queries.
#
# Report values are display strings that follow the member's account language (Datarc API notes); parse fields are
# schema primary names. The comparison is made only when every reported measure is a schema measure name, which shows
# that this report spells fields the way the parse does; otherwise nothing is asserted. The handler holds the schema, so
# it records the gaps in `_parse.fields_not_in_report` and the trimmer rebuilds the sentence from that field. A dimension
# word that only qualifies a parsed value (「种类大类=女装」) is a filter, not a grouping, and is skipped. The parse marks that in
# two ways: the value token carries the dimension as its field, or the dimension is followed by an operator token and a
# value token without a field. 116 on 1.7.43, t_ec9f8c06a7d0: 「种类大类为女装 营业额 每月」 parsed the second way, its report
# listed the filter 「种类大类为女装」, and the note still called 种类大类 an unlisted grouping on a correctly filtered result.
# 30-day dry run on 116 (146 stored single results): no measure gap (the 月坪效 case was a batch item, which events do not
# store); dimension gaps were 门店面积 15, 种类大类 5, 品牌 2, and 门店 3 (the parse of 「门店数」). Whether the engine inserted
# a reported measure cannot be told from the parse (「各门店 月坪效」 bound no measure token), so that is never claimed.

FIELDS_NOT_IN_REPORT = "fields_not_in_report"


def _condition_field_positions(tokens: list[object]) -> set[int]:
    """Positions of dimension tokens that open a condition (dimension, operator, value), like 「种类大类为女装」.

    Operators also cover aggregation words (「门店面积 总计」 parses 总计 as `op_sum`); those are not conditions.
    """
    worded = [(index, token) for index, token in enumerate(tokens) if isinstance(token, dict) and str(token.get("word") or "").strip()]
    return {
        index
        for (index, token), (_, operator), (_, value) in zip(worded, worded[1:], worded[2:], strict=False)
        if token.get("kind") == "dimension" and operator.get("kind") == "operator" and value.get("kind") in _FILTER_VALUE_KINDS
    }


def fields_not_in_report(tokens: object, query_explain: object, *, schema_measures: Iterable[str]) -> dict[str, list[str]]:
    """Return the parsed measure and dimension fields the structured report does not list; empty when unprovable."""
    if not isinstance(tokens, list) or not isinstance(query_explain, dict):
        return {}
    reported_measures = query_explain.get("measures")
    known = {str(name) for name in schema_measures}
    if not isinstance(reported_measures, list) or not reported_measures or not all(str(m) in known for m in reported_measures):
        return {}
    measures = {str(m) for m in reported_measures}
    reported_dimensions = query_explain.get("dimensions")
    # Datarc omits keys that took no effect, so a structured report without `dimensions` grouped by nothing.
    dimensions = {str(d) for d in reported_dimensions} if isinstance(reported_dimensions, list) else set()
    entries = [(index, t) for index, t in enumerate(tokens) if isinstance(t, dict) and t.get("field")]
    value_fields = {str(t["field"]) for _, t in entries if t.get("kind") in _FILTER_VALUE_KINDS}
    condition_fields = _condition_field_positions(tokens)
    gaps: dict[str, list[str]] = {"measures": [], "dimensions": []}
    for index, token in entries:
        field = str(token["field"])
        if token.get("kind") == "measure" and field not in measures and field not in gaps["measures"]:
            gaps["measures"].append(field)
        elif (
            token.get("kind") == "dimension"
            and index not in condition_fields
            and field not in value_fields
            and field not in dimensions
            and field not in gaps["dimensions"]
        ):
            gaps["dimensions"].append(field)
    return {key: names for key, names in gaps.items() if names}


def _quoted(names: Iterable[object]) -> str:
    return ", ".join(f"「{name}」" for name in names) or "none"


def fields_not_in_report_note(gaps: object, query_explain: object) -> str:
    """Name parsed fields that the structured report does not list, with what it lists instead."""
    if not isinstance(gaps, dict):
        return ""
    report = query_explain if isinstance(query_explain, dict) else {}
    parts: list[str] = []
    if measures := [str(m) for m in gaps.get("measures") or []]:
        parts.append(f"measure {_quoted(measures)} (executed measures: {_quoted(report.get('measures') or [])})")
    if dimensions := [str(d) for d in gaps.get("dimensions") or []]:
        parts.append(f"dimension {_quoted(dimensions)} (executed grouping: {_quoted(report.get('dimensions') or [])})")
    if not parts:
        return ""
    return (
        "Your query text names these fields, but the executed query (`query_explain`) does not use them: "
        + "; ".join(parts)
        + ". The returned values are the executed measures, and the rows are grouped only by the executed dimensions. "
        "Judge from the request whether each field was needed. If it was, these rows do not answer it: requery with a measure "
        "or field the schema offers, or tell the user the data cannot be queried that way, and never label the returned values "
        "with a field the executed query does not use or describe the rows as split by it. "
        "If the word was incidental, use the rows as returned."
    )


def parse_gap_notes(parse: object, query_explain: object) -> list[str]:
    """Build the `_preflight_hint` sentences about parsed words the engine report does not account for.

    Shared by the bi_query handler and the result-budget trimmer so both produce the same sentences from fields.
    """
    if not isinstance(parse, dict):
        return []
    words = [str(word) for word in parse.get("unknown_words") or []]
    values = unapplied_filter_values(parse, query_explain)
    notes: list[str] = []
    if words:
        notes.append(unknown_words_note(words))
    if values:
        notes.append(dropped_filter_note(values))
    elif words and engine_applied_no_filter(query_explain):
        notes.append(unfiltered_fact_note(words))
    if field_note := fields_not_in_report_note(parse.get(FIELDS_NOT_IN_REPORT), query_explain):
        notes.append(field_note)
    return notes


# ── No recognized metrics + engine-inserted default ⇒ fallback column, withheld from delivery ───────────
#
# 116 1.3.47 dogfood (2026-09-25): "2025 年的 IPT 是多少" yielded wrong numbers in 2 of 3 turns. datarc did not recognize IPT,
# filtered by literal condition "pit", inserted default metric "营业额", and returned 2,297,450 (a small revenue subset);
# the model used it **as a calculation factor**, not an answer: traffic yielded 7,197 元, revenue 0.098 元/人次, with correct formulas.
# Even with the wrong-scope warning above, the model used it. ⇒ The tool decides: withhold this column.
#
# Criteria use only structured fields, not words: ① no metric token in parse results (prop like `m12`); ② genuine unknown words;
# ③ engine-reported execution definitions still contain metrics it inserted. All three imply those metrics' columns are fallback.
# Leave text-only definitions unchanged (no structured `measures`); no facts means no assertions.
# 116 dry run over 30 days: 101/2504 results (4.0%) matched; unknown words were all IPT/客单/客单价/ipt/每单金额.
#
# Keep dimension columns (a request for each store's XX may only need a list); remove data-file references too: both wrong-answer turns
# used this result's CSV in sandbox calculations, so removing only inline rows would achieve nothing.

_ARTIFACT_REF_KEYS = ("artifact_id", "_ref_artifact_id", "artifact_filename", "_debug_csv_filename", "_how_to_use_in_sandbox")


def _is_measure_prop(prop: object) -> bool:
    text = str(prop or "")
    return len(text) > 1 and text[0] == "m" and text[1:].isdigit()


def fallback_measure_columns(tokens: Iterable[object], unknown_words: Iterable[str], query_explain: object, columns: Iterable[object]) -> list[str]:
    """该隐去的兜底指标列（按结果列名）。不满足三条判据时返回空。"""
    if not list(unknown_words):
        return []
    if any(_is_measure_prop(getattr(t, "prop", None) if not isinstance(t, dict) else t.get("prop")) for t in tokens):
        return []
    measures = query_explain.get("measures") if isinstance(query_explain, dict) else None
    if not isinstance(measures, list) or not measures:
        return []
    names = {str(m).strip() for m in measures if str(m).strip()}
    return [str(col) for col in columns or [] if str(col).split(" / ")[0].strip() in names]


def withhold_fallback_measures(result: dict, fallback_columns: list[str], words: Iterable[str], measures: Iterable[str]) -> None:
    """就地从结果里拿掉兜底列与数据文件引用，并在 `_preflight_hint` 里说明。

    The hint must not keep pointing at a data file the result no longer has. Regression check (the original regression): the
    preview sentence still said to read the remaining rows from the artifact, so compute passed another query's
    artifact to the sandbox; about ten calls read the wrong file and two failed on a missing column.
    """
    from bowhead_core.tool_result_budget import without_data_file_hint

    drop = set(fallback_columns)
    result["columns"] = [c for c in result.get("columns") or [] if c not in drop]
    rows = result.get("rows")
    if isinstance(rows, list):
        kept = [{k: v for k, v in row.items() if k not in drop} if isinstance(row, dict) else row for row in rows]
        result["rows"] = [] if not result["columns"] else kept
    for key in _ARTIFACT_REF_KEYS:
        result.pop(key, None)
    result["_withheld_fallback_measures"] = sorted(drop)
    quoted = "".join(f"「{w}」" for w in words)
    engine = "、".join(f"「{m}」" for m in measures)
    note = (
        f"⚠ 这次查询里没有认出任何指标（{quoted} 不是数据里能直接查的指标），引擎自己补了默认指标{engine}——"
        "那一列不是你要的数，已从结果里拿掉，**不要**用它参与任何计算。"
        "这条结果因此不提供数据文件（artifact_id）：能用的只有上面列出的行，**不要**拿别的查询的 artifact_id 当作它的数据。"
        "要算这个指标，先确认它由哪些原始指标构成（bi_schema / bi_concept_resolve），再用那些指标重查。"
    )
    prev = without_data_file_hint(
        result.get("_preflight_hint"),
        truncated=bool(result.get("_rows_truncated")),
        shown=result.get("_rows_inline_count", len(result.get("rows") or [])),
        full=result.get("_rows_full_count", result.get("row_count")),
    )
    result["_preflight_hint"] = f"{prev}\n{note}" if prev else note

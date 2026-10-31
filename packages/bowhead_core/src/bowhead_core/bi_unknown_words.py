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
    """就地从结果里拿掉兜底列与数据文件引用，并在 `_preflight_hint` 里说明。"""
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
        "那一列不是你要的数，已从结果和数据文件里拿掉，**不要**用它参与任何计算。"
        "要算这个指标，先确认它由哪些原始指标构成（bi_schema / bi_concept_resolve），再用那些指标重查。"
    )
    prev = result.get("_preflight_hint")
    result["_preflight_hint"] = f"{prev}\n{note}" if prev else note

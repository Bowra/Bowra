"""Single authority for citation marker syntax (citation-audit sprint, M1).

gen3 ``值 [N]`` is the ONLY persisted inline citation form going forward.
Everything in this module exists to serve that invariant:

- ``bind_to_numbered``  — bind plain prose values to ``[N]`` against the
  per-turn :class:`~bowhead_core.sources_resolver.TurnSourcesRegistry`
  numbering space (replaces the gen2 ``[num:V:U:src:S]`` autowrap output;
  binding conditions — precision window / ambiguous-drop / adjacent-``[N]``
  skip — are reused verbatim from the card-path binder).
- ``convert_gen2_src`` — ingest converter: peer/legacy ``[num:…:src:S]`` /
  ``[ref:S]`` markers → ``[N]`` (explicit ``:src:`` attribution stays
  content-addressed — never downgraded to value inference; #11 contract).
- ``strip_legacy``     — defensive seam: render/bake any retired syntax into
  plain formatted text (unit PRESERVED — the egress salvage that dropped
  units must never be reintroduced here).
- ``strip_untrusted_numbered`` — drop model-written bare ``[N]`` from fresh
  writer output before binding (A9 residual: read_report echoes give the LLM
  ``[N]`` shapes to copy; only ``_resolve_cites``/binder may mint them).

M3（读端下线）已完成：四路发射端的 gen2 分支、`BOWHEAD_GEN2_MARKERS` kill-switch、
FE 的四个 gen2 解析 pass 全部删除。本模块是退役语法的**唯一**在线知识——
`strip_legacy` 作为永久兜底 seam 留存（历史 SSE 事件重放 / image-revert / 外部导入
仍可能带进旧语法；渲染层永不显示 raw marker 是硬底线），语义与 FE
`lib/citation-marks.ts` 双端 golden 互锁（`__fixtures__/citation-parity.json`）。
设计见 docs/sprints/citation-audit-2026-07-16/tech-design.md。
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Collection
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bowhead_core import markdown_tables as _md_tables
from bowhead_core.canvas_composers.answer_decomposer import (
    _AUTOWRAP_PLAIN_RAW,
    _parse_value_to_float,
    _resolve_value,
)
from bowhead_core.sources_resolver import (
    _NUM_SMUSHED_RE,
    _NUM_STANDARD_RE,
    _alloc_source_index,
    _build_inv_lookup,
    resolve_in_text,
)

__all__ = [
    "CITATION_PROSE_KEYS",
    "NUMBERED_RE",
    "bake_changed_payload_strings",
    "bake_payload_strings",
    "bind_to_numbered",
    "convert_gen2_src",
    "format_marker_value",
    "CITE_MARKER_RE",
    "has_legacy_markers",
    "strip_cite_markers",
    "strip_legacy",
    "strip_untrusted_numbered",
]

logger = logging.getLogger(__name__)

# ── gen3 (current) ───────────────────────────────────────────────────────────
# [N] numbered citation — the integer indexes the report's sources entries.
NUMBERED_RE = re.compile(r"\[(\d{1,3})\]")

# `[cite:N]` is the chat-path citation marker (Lead final_answer), occasionally leaking into canvas
# narrative. Canvas and exports have no cite resolver: it references a chat turn-scoped registry,
# unavailable in both places, and renders as noise. Omit it from all display surfaces.
#
# ⚠ Match only `[cite:N]`; never touch `[N]` (`NUMBERED_RE`), the canonical citation number,
# which must display normally.
# ⚠ Cross-language duplicate constant with FE lib/citation-marks.ts CITE_MARKER_RE,
# whose TS source literal is compared by test_cite_marker_parity.py.
# ⚠ Deliberately restrict to `[0-9]`, not `\d`: Python's `\d` matches fullwidth ０-９, while JS matches
# only ASCII. FIGURE_CAPTION_PREFIX_RE previously diverged on this for 648/986 cases.
CITE_MARKER_RE = re.compile(r"\[cite:[0-9]{1,3}\]")


def strip_cite_markers(text: str) -> str:
    """把 chat 面的 `[cite:N]` 从要显示/导出的文本里剥掉（不动 `[N]`）。"""
    return CITE_MARKER_RE.sub("", text)


# ── retired syntax (strip/convert only — never emitted) ─────────────────────
# plain [num:V] / [num:V:U] / [num:V:U:] (trailing empty unit) — no :src:.
_NUM_PLAIN_RE = re.compile(r"\[num:([^:\[\]]+?)(?::([^:\[\]]*))?\]")
# catch-all for malformed [num:…] remnants after the shaped passes ran.
# ⚠ The character class must exclude `[` (not `[^\]]*`); otherwise each `[num:` start consumes the remaining text
# then backtracks character by character: M starts × L length = O(L²). Implementation review measured `"[num:"*20000` (100KB)
# spending 4.5s of CPU at this seam, attached via bake_payload_strings to the async gateway's PATCH /canvas/{id}/sections/{id};
# synchronously burning the event loop halts every tenant's requests. Any authenticated user can disable the gateway
# with one request. Excluding `[` reduces the same input to 0.0003s (≈15,000×), with byte-identical output
# for all valid marker shapes (206 dense corpus lines + eight manual shapes, zero differences).
# Sole behavior change: malformed `[num:a[b]` remnants containing `[` no longer match (previously baked to `a[b`,
# now left raw). Both leak; production corpus had zero matches, not worth restoring quadratic complexity.
# The first three passes already exclude `[` in their character classes; this was the sole exception.
_NUM_CATCHALL_RE = re.compile(r"\[num:([^\[\]]*)\]")
_CHART_RE = re.compile(r"\s?\[chart:[^\]]+\]")
_FACT_RE = re.compile(r"\s?\[(?:fact|bi|memory):[^\]]+\]")
_REF_STRIP_RE = re.compile(r"\s?\[ref:[^\]]+\]")

# i18n-v1 enum units (language_directive taught these; subsystem retires with
# this sprint) + µ-era ratio — mapped to the FE `stripInlineNumMarkers`
# semantics (md.ts: ratio→倍) so BE/FE bake output stays aligned.
_ASCII_UNIT_MAP = {
    "percent": "%",
    "ratio": "倍",
    "rate": "倍",  # Same mapping as FE md.ts (aligned in impl-review A-P3).
    "CNY": "元",
    "USD": "美元",
    "EUR": "欧元",
    "JPY": "日元",
    # Keep the persisted currency vocabulary aligned with chart units.
    "GBP": "英镑",
    "HKD": "港币",
    "TWD": "新台币",
    "KRW": "韩元",
    "SGD": "新加坡元",
    "count": "",
    "duration_days": "天",
    "duration_hours": "小时",
    # symbol units attach directly ("17.8%", never "17.8 %")
    "%": "%",
    "‰": "‰",
}

# Currency enum units combine raw value + currency code (`[num:7016893124:CNY]`), unlike
# display-shaped units such as `亿元`/`万元`/`%`. Their values are unscaled; concatenating a suffix
# bakes unreadable static text such as `7016893124元` (M3-1 found this in production sec_553d83cd99ce:
# the deposit KPI card rendered as `¥70.17亿` before migration, but became an eleven-digit bare number afterward).
# Thus scale currency units when baking: this is r3's intended bake = FE formatNumValue semantics.
# M1 inherited unit preservation but omitted scaling (M2's recorded invalidation of the r3 note was mistaken:
# only the per-owner locale dimension was invalidated, not scaling).
# Follow FE formatNumValue's zh path (persisted static text needs a fixed locale;
# production reports are all zh-CN, and _ASCII_UNIT_MAP's USD→美元 already chooses the zh side).
_CURRENCY_SCALE_UNITS = frozenset(
    {"CNY", "USD", "EUR", "JPY", "GBP", "HKD", "TWD", "KRW", "SGD"},
)  # == units.KNOWN_CURRENCIES, locked by test_units_contract

# Counter/unit token that may immediately follow a scale-suffixed number
# ("9627万" + "元"). The binder swallows it so `[N]` lands AFTER the unit —
# otherwise "9627万 [1]元" (r3 review P1-4).
# ⚠ Python alternation picks the leftmost match: multi-character units must precede their single-character prefixes (impl-review A-P1-1:
# 「人」 masked 「人次」, producing "9627万人 [1]次"). Sort by descending length.
# Narrow set swallowed across whitespace (see _bind_line): yuan-family currency units, with no word-splitting risk.
_SPACED_UNIT_SWALLOW_RE = re.compile(r"^(?:亿元|万元|美元|元)")

_UNIT_SWALLOW_RE = re.compile(
    r"^(人次|架次|场次|班次|台次|美元|亿元|万元"
    r"|元|家|个|次|人|天|单|件|台|辆|吨|套|户|笔|倍|条|款|间|种|亩|批|场|站|页|篇|部|支|架|艘)",
)
# a legit [N] right after (with optional whitespace) — the writer already
# cited this number explicitly; binding again would double-mark (#11).
_ADJACENT_NUMBERED_RE = re.compile(r"^\s*\[\d{1,3}\]")

# Three helpers for units-in-table-headers. Share Markdown table criteria with verification via markdown_tables;
# do not duplicate them here (drift counts numbers only in the denominator, not the numerator; see that module's docstring).
_TABLE_SEP_RE = _md_tables.TABLE_SEP_RE
_table_cell_spans = _md_tables.cell_spans
_table_header_units = _md_tables.header_units
# Bare cell numbers (no scale/currency/percentage): _AUTOWRAP_PLAIN_RAW excludes them.
_BARE_CELL_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.\[:])(\d[\d,]*(?:\.\d+)?)")
# An immediately following unit means the number has its own unit; let the regular pass handle it instead of parsing again with header units.
_UNIT_AFTER_NUM_RE = re.compile(r"^\s*(?:亿元|万元|亿|万亿|千万|百万|万|千|%|‰|元|个|家|pp|倍|[KMBT])")
# Header unit → scale suffix recognized by _parse_value_to_float. Explicit closed table; no prefix guessing:
# 「万元」 has scale 「万」, but _parse_value_to_float's suffix table lacks 「万元」 (observed returning None).
_HEADER_UNIT_TO_SCALE: dict[str, str] = {
    "亿元": "亿",
    "万元": "万",
    "亿": "亿",
    "万": "万",
    "%": "%",
    "pp": "pp",
    "元": "",
    "个": "",
    "家": "",
    "倍": "",
}

# markdown line shapes **the binder** must never touch (headings / blockquotes /
# figure anchors). Code fences are tracked separately.
#
# ⚠ Only for bind_to_numbered: a typography rule (these lines should not carry inline citations).
# strip_untrusted_numbered has its own _STRIP_SKIP_LINE_PREFIXES: a security rule
# with opposite polarity; sharing opens a hole (see its comments).
#
# ★ 2026-08-12: remove "|" from this list. _collect_unverified stopped skipping | lines on 2026-08-10
# to verify table numbers, but the binder still skipped them: they counted only in the denominator, never the
# numerator. pct = verified/(verified+unverified) was systematically low for table-heavy reports: one correct
# table with a total row outside _is_derivable (it lacks a+b) could drop pct from 100 to 50.
# The typography rationale is also obsolete: [N] now renders as a PM decoration chip normally inside cells.
#
# Security depends on ordering, not line type: run strip_untrusted_numbered before the binder
# (report_writer_loop:398→:406 for direct output, :1180→:1188 for the tool loop).
# No legitimate bare [N] exists anywhere at that point, so fabricated table [N] markers are stripped too;
# only those minted by the binder afterward are real. test_citation_bind_table_lines.py structurally guards this order;
# moving the binder before stripping opens the hole where forged [N] survives stripping.
_BINDER_SKIP_LINE_PREFIXES = ("#", ">", "[figure:")

# Strip-side skip list: only code fences (tracked separately) + figure-anchor lines.
#
# Why the binder's list cannot be reused (impl-review security P1-1, confirmed in this batch): opposite polarity;
# binder means do not add [N] to these lines (typography), strip means do not leave forged [N] (security).
# Sharing one constant creates the worst pairing: binder never mints real [N] in tables/headings, so bare [N] in
# those lines can only be model fabrications, yet strip exempts exactly those lines. Observed: a 50-line table with
# no tool calls and [1] everywhere produced verified=50 / pct=100%, inventing trusted citations. BI reports naturally
# contain many tables; this is not an edge-case input.
#
# Why stripping [N] from tables/headings cannot harm legitimate citations: this function consumes only fresh model output, before
# all citation minters. Both report_writer_loop paths use this order
# `strip_untrusted_numbered` → `_resolve_cites` → `_apply_citation_pass`(binder)
# (once in _direct_compose and once in the tool loop). All legitimate [N] markers are minted downstream,
# so at strip time none exists anywhere in the text, regardless of line type.
#
# ★ Ordering is this module's sole security anchor, especially since 2026-08-12: the binder's list no longer
# skips | lines (table numbers must enter the verified numerator), invalidating the rule that table [N] must be forged.
# See the order guard in test_citation_bind_table_lines.py.
#
# Preserving fences is necessary (A-P2-3): a[1] inside a code block is an array subscript, not a citation.
_STRIP_SKIP_LINE_PREFIXES = ("[figure:",)


def format_marker_value(value: str, unit: str) -> str:
    """Render a retired marker's (value, unit) as plain text, unit PRESERVED.

    CJK / %-family units attach directly (``5.2亿元`` / ``17.8%``); i18n-v1
    ASCII enum units map to their CJK/symbol equivalents; unknown ASCII units
    join with a space. NEVER returns the bare value when a unit exists — the
    egress salvage that silently dropped units is the bug this replaces.
    """
    v = (value or "").strip()
    u = (unit or "").strip()
    if not u:
        return v
    # impl-review A-P2-2 (decision recorded in tech-design §3.4): bake fractional ASCII enum percent values
    # (|v|≤1, i18n-v1 contract=raw value) by multiplying ×100, matching FE
    # md.ts _formatNumDisplay's ≤1 criterion; keep already-scaled values (30:percent) unchanged.
    # Otherwise existing markers displayed by FE as 13.2% become persisted 0.132% at the ingress seam.
    # Do not scale the % symbol form ([num:1:%] means 1%; display values are already scaled).
    if u == "percent":
        try:
            fv = float(v)
            if abs(fv) <= 1:
                return f"{fv * 100:g}%"
        except ValueError:
            pass
        return f"{v}%"
    if u in _CURRENCY_SCALE_UNITS:
        return _format_currency(v, u)
    mapped = _ASCII_UNIT_MAP.get(u)
    if mapped is not None:
        return f"{v}{mapped}"
    if u.isascii():
        return f"{v} {u}"
    return f"{v}{u}"


def has_legacy_markers(text: str) -> bool:
    """True if any retired marker syntax survives in `text` (assertion helper)."""
    if not isinstance(text, str) or not text:
        return False
    return bool(
        _NUM_CATCHALL_RE.search(text) or _REF_STRIP_RE.search(text) or _CHART_RE.search(text) or _FACT_RE.search(text),
    )


def strip_legacy(text: str) -> str:
    """Bake every retired marker into plain text (defensive seam).

    Ordered passes — ``:src:`` shapes first (they contain the plain shape as a
    prefix), then plain, then a catch-all for malformed remnants, then the
    non-num families. Output for num-shapes is ``format_marker_value`` — the
    unit is always preserved.
    """
    if not isinstance(text, str) or not text:
        return text

    def _std(m: re.Match[str]) -> str:
        return format_marker_value(m.group(1), m.group(2))

    def _smushed(m: re.Match[str]) -> str:
        # value+unit live smushed in group 1 ("11.91亿") — already display-shaped.
        return (m.group(1) or "").strip()

    def _plain(m: re.Match[str]) -> str:
        return format_marker_value(m.group(1), m.group(2) or "")

    def _catchall(m: re.Match[str]) -> str:
        # malformed remnant — best effort: first segment is the value, second
        # (when present and not a src tail) is the unit.
        body = m.group(1) or ""
        segs = [s for s in body.split(":") if s != ""]
        if not segs:
            return ""
        if segs[0] == "src":
            # Pure src remnants such as [num::src:tl] have no salvageable value; remove them entirely
            # (impl-review A-P3: the old implementation leaked garbage text "src tl").
            return ""
        value = segs[0]
        unit = segs[1] if len(segs) > 1 and segs[1] != "src" and not segs[1].startswith("src") else ""
        return format_marker_value(value, unit)

    out = _NUM_STANDARD_RE.sub(_std, text)
    out = _NUM_SMUSHED_RE.sub(_smushed, out)
    out = _NUM_PLAIN_RE.sub(_plain, out)
    out = _NUM_CATCHALL_RE.sub(_catchall, out)
    out = _REF_STRIP_RE.sub("", out)
    out = _CHART_RE.sub("", out)
    out = _FACT_RE.sub("", out)
    return out


def strip_untrusted_numbered(
    text: str,
    *,
    keep: Collection[int] = (),
    mask: Callable[[int], str] | None = None,
) -> str:
    """Remove bare ``[N]`` tokens from FRESH model output (append path only).

    ``keep``：更新路用——旧正文里本来就有的号是合法引用，原样留；只处理模型新写的号。
    ``mask``：不直接删，换成它返回的串（bake 用占位符接住，登记表就绪后再判能不能翻成真编号）。

    Legit ``[N]`` are minted downstream by ``_resolve_cites`` ([cite:N]) and
    the binder — anything already shaped ``[N]`` in raw model output is a
    read_report echo / hallucination riding a stale number (A9)。impl-review
    实锤这必须覆盖**全部** fresh 产出路（writer append + thin-path peer 文本 +
    composer peer payload——peer 手写死号 [1] 曾被渲成可点「可信引用」并刷
    verified 计数）。NOT for update/edit flows（`section_id` 显式更新的回读
    内容含合法 [N]，剥了=引用团灭——impl-review C-P1-1）and NOT for persisted
    content。**只有 fence / [figure: 锚行不动**（A-P2-3：代码块里的 ``a[1]`` 是数组
    下标不是引用）——表格/标题/blockquote 照剥：那是 binder 的排版跳过清单，借来当
    安全清单会开洞（M3 impl review 安全面 P1-1 实锤：零工具调用的表格可造出
    verified=100%），详见 ``_STRIP_SKIP_LINE_PREFIXES`` 注释。
    """
    if not isinstance(text, str) or not text:
        return text

    def _sub(m: re.Match[str]) -> str:
        n = int(m.group(1))
        if n in keep:
            return m.group(0)
        return mask(n) if mask is not None else ""

    out_lines: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        st = line.lstrip()
        if st.startswith("```"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence or not st or st.startswith(_STRIP_SKIP_LINE_PREFIXES):
            out_lines.append(line)
            continue
        out_lines.append(NUMBERED_RE.sub(_sub, line))
    return "\n".join(out_lines)


# Prose text keys that may legally carry retired markers (migration-census allowlist,
# key-dimension projection of the tech-design §3.4 per-(kind,path) table). Structural IDs/code fields
# (sql/code/query/label/artifact_id/entries/source_refs…) are never included: census
# confirmed that their many bare art_ IDs are normal data (r3 migration P1-1).
CITATION_PROSE_KEYS = frozenset(
    {
        "markdown",
        "content",
        "body",
        "detail",
        "text",
        "thesis",
        "narrative",
        "title",
        "caption",
        "subcaption",
        "callout_text",
        "expected_impact",
        "rationale",
        "action",
        "val",
        "sub",
        "delta",
        "quantity",
        "value",
        # Production census 2026-07-17 (citation-audit M2): writer-invented impact.magnitude
        # (outside schema) contained [num:] markers; add this key. No structural field has this name repository-wide.
        "magnitude",
        "summary",
        "reason",
        "exec_summary",
        "owner",
        "timeline",
    },
)
_BAKE_TEXT_KEYS = CITATION_PROSE_KEYS


def strip_cite_payload_strings(payload: Any) -> Any:
    """Recursively strip chat-scope ``[cite:N]`` markers from a payload's PROSE
    string fields（同 ``bake_payload_strings`` 的 key 白名单）。

    source-accessibility review F2 的写入口守卫：``[cite:N]`` 的 N 是 **turn
    作用域**编号，而 patch 不改 ``section.turn_id``。跨轮 patch 带着本轮注册表的
    marker 落进别轮的段落，读侧会拿段落 turn 的第 N 条来渲 —— 标错来源比藏起来
    更糟。调用方（agent patch 工具）只在跨轮时调用；幂等。
    """
    if isinstance(payload, dict):
        return {
            k: (strip_cite_markers(v) if isinstance(v, str) and k in _BAKE_TEXT_KEYS else strip_cite_payload_strings(v)) for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [strip_cite_payload_strings(v) for v in payload]
    return payload


def bake_payload_strings(payload: Any) -> Any:
    """Recursively bake retired markers out of a section payload's PROSE string
    fields (`strip_legacy` per string; key whitelist ``_BAKE_TEXT_KEYS``).

    The permanent ingress seam (M1-6): revision rollbacks re-write prevPayload
    verbatim through ``patch_section``, users paste, models手滑 — whatever the
    route, retired syntax gets baked at the single write chokepoint instead of
    surviving in the DB. Idempotent; marker-free payloads pass through
    unchanged. Also the future migration's bake core (same semantics, single
    source of truth).
    """
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            if isinstance(v, str) and k in _BAKE_TEXT_KEYS:
                out[k] = strip_legacy(v)
            else:
                out[k] = bake_payload_strings(v)
        return out
    if isinstance(payload, list):
        return [bake_payload_strings(v) for v in payload]
    return payload


_MISSING = object()


def bake_changed_payload_strings(
    canonical_payload: Any,
    incoming_patch: Any,
    projected_current: Any,
    *,
    normalize_unchanged: Callable[[str], str] | None = None,
) -> Any:
    """Bake retired markers only in prose values actually changed by a viewer.

    ``canonical_payload`` is the provenance-restored merge result, while
    ``incoming_patch`` and ``projected_current`` are viewer-safe shapes. An
    unchanged alias-bearing value must retain its canonical source marker;
    otherwise a full-form no-op PATCH would silently erase provenance. Newly
    supplied or edited prose still passes through the permanent ingress seam.
    """

    def walk(canonical: Any, incoming: Any, projected: Any) -> Any:
        if incoming is _MISSING:
            return canonical
        if isinstance(canonical, dict):
            incoming_map = incoming if isinstance(incoming, dict) else {}
            projected_map = projected if isinstance(projected, dict) else {}
            out: dict[str, Any] = {}
            for key, value in canonical.items():
                incoming_value = incoming_map.get(key, _MISSING)
                projected_value = projected_map.get(key, _MISSING)
                if isinstance(value, str) and key in _BAKE_TEXT_KEYS and incoming_value is not _MISSING:
                    if incoming_value == projected_value:
                        out[key] = normalize_unchanged(value) if normalize_unchanged is not None else value
                    else:
                        out[key] = strip_legacy(value)
                else:
                    out[key] = walk(value, incoming_value, projected_value)
            return out
        if isinstance(canonical, list):
            incoming_list = incoming if isinstance(incoming, list) else []
            projected_list = projected if isinstance(projected, list) else []
            return [
                walk(
                    value,
                    incoming_list[index] if index < len(incoming_list) else _MISSING,
                    projected_list[index] if index < len(projected_list) else _MISSING,
                )
                for index, value in enumerate(canonical)
            ]
        return canonical

    return walk(canonical_payload, incoming_patch, projected_current)


def convert_gen2_src(
    text: str,
    *,
    sources: list[dict[str, Any]],
    id_to_index: dict[str, int],
    tool_invocations: list[Any] | None = None,
) -> str:
    """Ingest converter: ``[num:…:src:S]`` / ``[ref:S]`` → ``值 [N]`` against the
    shared numbering space (a :class:`TurnSourcesRegistry`'s ``sources`` +
    ``id_to_index``). Explicit ``:src:`` attributions keep their
    content-addressed binding — never downgraded to value inference (#11).
    """
    new_text, _, _ = resolve_in_text(
        text,
        sources=sources,
        id_to_index=id_to_index,
        tool_invocations=tool_invocations,
    )
    return new_text


def _js_to_fixed(n: float, digits: int) -> str:
    """JS ``Number.prototype.toFixed`` 的等价实现（**双端同源硬要求**）。

    Python 的 f-string `.Nf` 走 round-half-**even**（IEEE 默认），JS `toFixed` 走
    round-half-**away**；两者在精确二进制 tie 上分家——impl review 实锤：
    `[num:12500:CNY]` → BE `1.2万元` / FE `1.3万元`（12500/1e4 = 1.25 恰好可精确
    表示）。万档每 10000、亿档每 1250万 就撞一次，是日常值域。

    `Decimal(float)` 取的是浮点的**精确二进制展开**（与 JS toFixed 的输入同一个
    值），再 ROUND_HALF_UP → 与 toFixed 逐例一致（含 1.005/2.675 这类「看着是 tie
    实际不是」的坑：两端都给 1.00 / 2.67）。
    """
    return str(Decimal(n).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def _format_currency(value: str, unit: str) -> str:
    """`raw 数值 + 货币代码` → zh 缩放显示形（`7016893124`+`CNY` → `70.17亿元`）。

    口径对齐 FE `formatNumValue` 的 zh 路径（万/亿分档 + 小数位），后缀走
    `_ASCII_UNIT_MAP`（元/美元/欧元/日元）而非 `¥` 前缀——语料里已有的 72 处
    `亿元` / 7 处 `万元` 标记就是这个约定，保持一致。非数值/非有限值原样加后缀
    （impl review P2-1：`[num:nan:CNY]` 曾让 `int(nan)` 抛 ValueError 打崩兜底 seam
    ——一个专为不受信文本而生的 seam 不能被脏输入打崩）。
    """
    suffix = _ASCII_UNIT_MAP[unit]
    try:
        n = float(value)
    except ValueError:
        return f"{value}{suffix}"
    if not math.isfinite(n):  # nan / inf: echo unchanged; never perform arithmetic.
        return f"{value}{suffix}"
    a = abs(n)
    if a >= 1_0000_0000:
        return f"{_js_to_fixed(n / 1_0000_0000, 2)}亿{suffix}"
    if a >= 1_0000:
        return f"{_js_to_fixed(n / 1_0000, 1)}万{suffix}"
    if n == int(n):
        return f"{int(n):,}{suffix}"
    return f"{Decimal(_js_to_fixed(n, 2)):,}{suffix}"


def bind_to_numbered(
    text: str,
    *,
    value_to_source: dict[float, str],
    sources: list[dict[str, Any]],
    id_to_index: dict[str, int],
    tool_invocations: list[Any] | None = None,
) -> str:
    """Bind plain prose numbers to ``[N]`` (the gen3 replacement for the gen2
    autowrap). Matching skeleton + binding conditions (precision window,
    ambiguous-drop, single-source-only) are the card-path binder's, reused
    verbatim; only the OUTPUT changed: ``值[+单位] [N]``.

    Line shapes that must not carry inline citations (headings / blockquotes /
    figure anchors / code fences) are skipped here — the skip rule lives in
    THIS module so all four producer paths share it. **表格行不在跳过之列**
    （2026-08-12）：跳着它会让表格数字只进 unverified 分母、进不了 verified 分子，
    trust 百分比对表格多的报告系统性偏低。理由与安全前提见 `_BINDER_SKIP_LINE_PREFIXES`。
    """
    if not text or not value_to_source:
        return text

    inv_lookup = _build_inv_lookup(tool_invocations or [])

    # Review F2: _build_value_to_source_map's map carries strong_keys (prioritize strong sources in weak-value windows);
    # ordinary dicts lack the attribute → None → _resolve_value keeps its old behavior.
    _strong_keys = getattr(value_to_source, "strong_keys", None)

    def _resolve_src(raw: str) -> str | None:
        f = _parse_value_to_float(raw)
        if f is None:
            return None
        if isinstance(f, tuple):
            return _resolve_value(f, value_to_source, strong_keys=_strong_keys)
        return value_to_source.get(f) or _resolve_value(
            f,
            value_to_source,
            raw_value_str=raw,
            strong_keys=_strong_keys,
        )

    def _bind_line(line: str) -> str:
        out_parts: list[str] = []
        pos = 0
        for rm in _AUTOWRAP_PLAIN_RAW.finditer(line):
            if rm.start() < pos:
                continue  # inside a previously swallowed unit region
            raw = rm.group(0)
            rest = line[rm.end() :]
            # unit-swallow: "9627万" followed by "元" → cite the whole
            # "9627万元" so `[N]` lands after the unit (never "9627万 [1]元").
            # Expression e2: whitespace may precede a unit (`2,460 元`, observed writer output); the scale branch's
            # in-group [^\S\n]* already allows this, including fullwidth U+3000. Swallow whitespace too,
            # so [N] follows the unit rather than splitting number [N] unit.
            # ⚠ Across whitespace, recognize only yuan-family units: swallowing single-character counters (家/人/天…) with whitespace
            # splits 家 from 「1,234,567 家长」, rendering 「家 [1]长」 (observed in the review
            # validation round). Counters still must immediately follow the number to be swallowed.
            _ws = len(rest) - len(rest.lstrip(" \t\u3000\xa0"))
            if _ws:
                unit_m = _SPACED_UNIT_SWALLOW_RE.match(rest[_ws:])
            else:
                unit_m = _UNIT_SWALLOW_RE.match(rest)
            swallowed = rest[: _ws + unit_m.end()] if unit_m else ""
            after = rest[len(swallowed) :]
            src = _resolve_src(raw)
            if src is None or _ADJACENT_NUMBERED_RE.match(after):
                # unbindable, or the writer already cited it (directly / after
                # its unit) — leave the number and its unit untouched.
                out_parts.append(line[pos : rm.end()])
                pos = rm.end()
                continue
            n = _alloc_source_index(
                src,
                sources=sources,
                id_to_index=id_to_index,
                inv_lookup=inv_lookup,
            )
            out_parts.append(line[pos : rm.start()])
            out_parts.append(f"{raw}{swallowed} [{n}]")
            pos = rm.end() + len(swallowed)
        out_parts.append(line[pos:])
        return "".join(out_parts)

    def _bind_bare_in_cell(cell: str, unit: str) -> str:
        """给「单位写在表头、格里是裸数字」的单元格绑 `[N]`。

        `_AUTOWRAP_PLAIN_RAW` 要求数字**自带**量级/货币/百分号才算显著数字，裸 `108350`
        一概不匹配 —— 而 116 全库 3,057 个表格数字（**占表格数字三分之二**）正是这种形态，
        它们能被核验侧的 `_apply_header_units` 够到（进 unverified 分母），却永远绑不上
        （进不了 verified 分子）。这里补齐绑定侧的同一件事。

        ⚠ 只拿表头单位去**解析**，绝不把单位写进正文 —— 输出是 `108350 [1]` 而不是
        `108350万元 [1]`。改写用户可见文本不是引用绑定该做的事。
        """
        scale = _HEADER_UNIT_TO_SCALE.get(unit)
        if scale is None:
            return cell
        parts: list[str] = []
        pos = 0
        for m in _BARE_CELL_NUM_RE.finditer(cell):
            if m.start() < pos:
                continue
            after = cell[m.end() :]
            # Regular passes handle numbers with their own units; do not rebind explicit writer citations.
            if _UNIT_AFTER_NUM_RE.match(after) or _ADJACENT_NUMBERED_RE.match(after):
                continue
            src = _resolve_src(f"{m.group(1)}{scale}")
            if src is None:
                continue
            n = _alloc_source_index(
                src,
                sources=sources,
                id_to_index=id_to_index,
                inv_lookup=inv_lookup,
            )
            parts.append(cell[pos : m.end()])
            parts.append(f" [{n}]")
            pos = m.end()
        parts.append(cell[pos:])
        return "".join(parts)

    def _bind_table_row(line: str, col_units: list[str | None]) -> str:
        spans = _table_cell_spans(line)
        if not spans:
            return line
        out: list[str] = []
        prev = 0
        for i, (a, b) in enumerate(spans):
            unit = col_units[i] if i < len(col_units) else None
            out.append(line[prev:a])
            out.append(_bind_bare_in_cell(line[a:b], unit) if unit else line[a:b])
            prev = b
        out.append(line[prev:])
        return "".join(out)

    out_lines: list[str] = []
    in_fence = False
    lines = text.split("\n")
    # Table line number: -1=outside, 0=header, 1=separator, >=2=data. Same positional criteria as verification
    # (_collect_unverified's table_line_no): placeholder | --- | --- | and actual separator rows
    # have indistinguishable syntax; only position distinguishes them.
    table_line_no = -1
    col_units: list[str | None] = []
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            table_line_no, col_units = -1, []
            out_lines.append(line)
            continue
        if in_fence or not stripped or stripped.startswith(_BINDER_SKIP_LINE_PREFIXES):
            table_line_no, col_units = -1, []
            out_lines.append(line)
            continue
        if stripped.startswith("|"):
            if table_line_no < 0:
                nxt = lines[idx + 1].lstrip() if idx + 1 < len(lines) else ""
                if _TABLE_SEP_RE.match(nxt):
                    # Header: collect each column's unit for subsequent data rows; do not bind the header itself.
                    col_units = _table_header_units(line)
                    table_line_no = 1
                    out_lines.append(line)
                    continue
                col_units = []
                table_line_no = 2
            elif table_line_no == 1:
                table_line_no = 2
                out_lines.append(line)  # Separator rows have no business numbers.
                continue
            bound_line = _bind_line(line)
            out_lines.append(_bind_table_row(bound_line, col_units) if col_units else bound_line)
            continue
        table_line_no, col_units = -1, []
        out_lines.append(_bind_line(line))
    bound = "\n".join(out_lines)
    _observe_bind_rate(text, bound)
    return bound


def _observe_bind_rate(before: str, after: str) -> None:
    """bind-rate 观测（`report_numbered_bound`）——**挂在 binder 这个唯一 seam 上**。

    为什么在这而不在各调用点（M3 impl review 删除安全性面 P2）：gen2 时代的
    `bowhead_inline_bind_total` 由 `answer_decomposer._bind_inline_num_sources` 发，
    随 gen2 一起删除后，只有 writer-loop 路补了 gen3 替代指标，card 路的三个绑定点
    （composer Phase-F / canvas_tools thin-path / streaming）**一个都没补**——恰好在
    binder 换代（gen2 autowrap → gen3 [N]）这一刻，最需要 bind-rate 的时候信号归零，
    而这正是 PRD 点名的 ship 观测信号（`kappa-streaming-canvas/prd-r2.md:320`：
    ratio 掉 >10% → warn；`unified-agentic-lead/retro.md:15`：BowheadInlineBindRateLow）。

    在调用点各发一次 = 给第 4 个调用点留下「忘记发」的伏笔（前三个已经忘了两个）。
    绑定只有这一条 seam，指标就该在这条 seam 上——加新调用点自动带观测。

    best-effort：观测永远不能打断绑定（metrics 未启用时 `observe` 内部即短路）。
    """
    try:
        from bowhead_core.metrics import observe as _obs

        n_bound = max(0, len(NUMBERED_RE.findall(after)) - len(NUMBERED_RE.findall(before)))
        _obs(
            "report_numbered_bound",
            float(n_bound),
            buckets=(0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0),
        )
    except Exception:  # pragma: no cover - Observation must not affect the main path.
        logger.debug("citation_marks.bind_rate_observe_failed", exc_info=True)

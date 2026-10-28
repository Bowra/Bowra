"""Shared report validation and deterministic composition of Agent-authored content."""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Any

from bowhead_core import markdown_tables as _md_tables
from bowhead_core.canvas_composer import _event
from bowhead_core.canvas_rank import canonical_order
from bowhead_core.canvas_store import MACHINE_REORDER_TURN_ID
from bowhead_core.events import raise_if_terminal_bus_failed
from bowhead_core.number_verifier import MINUS_IF_NOT_RANGE
from bowhead_core.sources_resolver import (
    get_turn_sources_registry,
    upsert_turn_sources_section,
)
from bowhead_core.terminal_llm_invocation import reject_unclassified_direct_mutation
from bowhead_router import Router

logger = logging.getLogger(__name__)

# Shared citation, figure and finalization helpers retain this import path.
# Report authors write directly; the retired prose-scribe loop is not a caller.


_ANCHOR_ONLY_LINE = re.compile(r"^[ \t]*\[figure:([a-zA-Z0-9_]+)\][ \t]*$")
_TRAILING_ANCHOR_RUN = re.compile(r"(?:[ \t]*\[figure:[a-zA-Z0-9_]+\])+[ \t]*$")
_FIGURE_ID_IN_RUN = re.compile(r"\[figure:([a-zA-Z0-9_]+)\]")
# Batch B2-4 counting: remove complete fenced blocks (``` / ~~~) before counting anchors; code brackets are not citations, as in `_strip_invalid_figure_anchors`.
_FENCED_BLOCK_RE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", re.MULTILINE | re.DOTALL)
_CAPTION_LINE = re.compile(r"^[ \t]*(?:\*\*)?图\s*\d")


def _strip_invalid_figure_anchors(md: str, valid_ids: set[str]) -> tuple[str, int]:
    """Drop `[figure:<id>]` anchor lines whose id is NOT a real figure section
    (+ the immediately-following `**图N**:` caption line, which would otherwise
    dangle). dogfood F9: the prompt forbids inventing ids, but the LLM is
    unreliable, so we enforce it deterministically here.

    review F4（表达面 e2 review 轮）：**行尾锚**也要接住——inline_hint 之后
    行尾锚是 LLM 真实产出形态（chat 面 74d0e81c3 已实证），peer 散文经
    payload 流入写手后同样会出现在报告正文里。行尾锚里：无效 id 剥 token
    保正文；有效 id 改写成独占一行。围栏内一律不动（代码里的方括号不是引用）。

    ⚠ 订正（2026-09-20，vitest 真跑前端解析器）：这里原本声称「归一成独占行，
    PM 渲染链 liftFigureAnchors 就能提升」——**在画布上不成立**。本函数产出的
    `正文\n[figure:x]` 与上文之间没有空行，CommonMark 把它并进上一段，而 PM 只
    提升「段落首个文本节点开头」的锚 ⇒ 照样漏成字面量（116 上这是占比最大的
    一类漏出，23/49）。当时的判据取自 **chat** 渲染器（`md.ts` 逐行切段，那边
    确实能渲）。**位置归一现在由存储写入口统一负责**
    （`figure_anchor_layout` + `CanvasStore` 五个写入口），本函数只保留「无效 id
    剥除」这一半语义，字节行为保持不变——chat 闸 `chat_figure_anchors` 依赖它
    「没什么可修就一个字节都不动」。
    Returns (cleaned_md, n_stripped)."""
    lines = md.split("\n")
    out: list[str] = []
    stripped = 0
    i = 0
    in_fence = False
    while i < len(lines):
        if lines[i].lstrip().startswith("```") or lines[i].lstrip().startswith("~~~"):
            in_fence = not in_fence
            out.append(lines[i])
            i += 1
            continue
        if in_fence:
            out.append(lines[i])
            i += 1
            continue
        m = _ANCHOR_ONLY_LINE.match(lines[i])
        if m and m.group(1) not in valid_ids:
            stripped += 1
            i += 1
            if i < len(lines) and _CAPTION_LINE.match(lines[i]):
                i += 1  # drop the orphaned caption too
            if i < len(lines) and lines[i].strip() == "":
                i += 1  # collapse the blank line left behind
            continue
        if not m:
            tm = _TRAILING_ANCHOR_RUN.search(lines[i])
            if tm and tm.start() > 0:
                prose = lines[i][: tm.start()].rstrip()
                ids = _FIGURE_ID_IN_RUN.findall(lines[i][tm.start() :])
                keep = [fid for fid in ids if fid in valid_ids]
                stripped += len(ids) - len(keep)
                if prose:
                    out.append(prose)
                for fid in keep:
                    out.append(f"[figure:{fid}]")
                i += 1
                continue
        # Mid-sentence anchors: `prose [figure:x] more prose` matches neither
        # `_ANCHOR_ONLY_LINE` nor `_TRAILING_ANCHOR_RUN` (the latter needs the
        # run at end of line), so an invalid id used to survive the stripper and
        # trip `write_report`'s `unknown_figure_ref` gate, which rejects the
        # whole report. Drop only the bad tokens and keep the prose; valid ids
        # are left byte-identical for the layout normalizer (and for the chat
        # gate, which relies on this function not touching clean input).
        line = lines[i]
        if "[figure:" in line:
            n_bad = sum(1 for fid in _FIGURE_ID_IN_RUN.findall(line) if fid not in valid_ids)
            if n_bad:
                line = _FIGURE_ID_IN_RUN.sub(lambda m: m.group(0) if m.group(1) in valid_ids else "", line)
                line = re.sub(r"[ \t]{2,}", " ", line).rstrip()
                stripped += n_bad
                if not line.strip():
                    i += 1
                    continue
                out.append(line)
                i += 1
                continue
        out.append(lines[i])
        i += 1
    # collapse any 3+ newline runs the removal may have created
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return cleaned, stripped


async def _emit_section(canvas_store, bus, canvas_id, turn_id, kind, payload, owner: str | None = None) -> tuple[str, Any]:
    raise_if_terminal_bus_failed(bus)
    stored = await canvas_store.append_section(
        canvas_id=canvas_id,
        kind=kind,
        payload=payload,
        by_turn_id=turn_id,
        owner=owner,
    )
    await bus.emit(
        _event(
            turn_id,
            "canvas.section_added",
            {"canvas_id": canvas_id, "section": stored.to_report_section(), "seq": stored.seq},
        )
    )
    raise_if_terminal_bus_failed(bus)
    return kind, stored


def _truncate_heading(text: str, limit: int = 160) -> str:
    """批 C (report-edit-audit P2-1): 标题/eyebrow 截断.

    原 `[:80]` 硬切在写库前发生, 页面/导出/列表/分享全是残标题
    (走查实锤: "…Profit Rate & Traffi" —— `Traffic` 被拦腰切且无省略号)。
    改为: 上限放宽到 160 + **词边界**截断(空格分词的文本回退到最后一个空格,
    CJK 无空格则硬切) + 省略号。存量残标题不回改(PRD 验收: 只保证新报告)。
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    # Retreat to whitespace only near the cutoff; pure CJK/long words without suitable boundaries retain the hard cut.
    if space > limit - 24:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _fmt_num(v: float) -> str:
    """写手证据里的数字。

    ★★ |v| < 1 走**有效数字**，不走固定两位小数。固定两位会把 0.0064 渲成 `0.01`、
    0.0004 渲成 `0.00`、-0.0004 渲成 `-0.00`——而写手是照着这些数写正文的：
    整列「同比变化率」在它眼里全是 `0.00`，它会写出「各月基本持平／无明显变化」。
    116 实测有 103 列的值全在这一档（多是以小数存的比率，0.0064 = 同比 0.64%）。
    前端轴刻度那侧（`lib/chart/format.ts` 的 `abbrTick`）是同一个毛病、同一种修法。
    """
    av = abs(v)
    if av >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if av >= 1e4:
        return f"{v / 1e4:.1f}万"
    if av > 0 and av < 1:
        # `.2g` avoids scientific notation in this range (0.0004 -> `0.0004`), using it only for |v| <1e-4
        # (1e-7 -> `1e-07`), where there is no better readable representation anyway.
        return f"{v:.2g}"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


#: Dimension column types for labels, matching nonnumeric `chart.types.ChartColumnType` members.
_SUMMARY_DIM_TYPES = frozenset({"categorical", "date", "geo"})

#: Maximum characters in one chart summary. 116: the old maximum was 392 characters; adding dimensions and column names
#: raised it to 900 (4.4x). Use 600: one-dimension/one-value charts (14 rows, ~210 characters) stay byte-identical;
#: only wide tables show fewer rows, gaining attributable entities on every row.
_SUMMARY_CHAR_BUDGET = 600

#: Column names containing scales: `营业额 / 万元`, `金额_亿元`, `数值(亿元)`, `平均营业额万元`.
#: 116 measured 63 such columns with values ≥1e4.
_SCALE_IN_NAME_RE = re.compile(r"[万亿千]")

# Match ANY `[cite:...]` content (not just digits) so a non-numeric label the LLM
# sometimes emits — e.g. `[cite:总体摘要]` (real dogfood leak 2026-06-24) — is caught
# and stripped instead of leaking raw into the report. `_sub` resolves digits / strips
# the rest.
_CITE_RE = re.compile(r"\[cite:\s*([^\]]*?)\s*\]")


def _resolve_cites(content: str, n_sources: int) -> str:
    """Resolve writer-emitted ``[cite:N]`` → ``[N]`` when N is a valid 1-based
    source number, else **strip** it (orphan / hallucinated number).

    The backend is authoritative: LLM marker emit is unreliable (the chat path
    moved to integers + backfill for exactly this reason), so a fabricated
    ``[cite:99]`` can never reach the rendered report. ``[N]`` is the canvas
    citation form the frontend decorates against the Sources section
    (sources[N-1])."""

    def _sub(m: re.Match[str]) -> str:
        raw = m.group(1).strip()
        if raw.isdigit():
            n = int(raw)
            return f"[{n}]" if 1 <= n <= n_sources else ""
        # non-numeric label / empty / multi (e.g. `[cite:总体摘要]`, `[cite:1,2]`):
        # strip rather than leak the raw marker into the rendered report.
        return ""

    return _CITE_RE.sub(_sub, content)


# Design P3: the report title is a top-level h1 section in the header zone
# (eyebrow -> h1 -> meta), NOT a `# Title` buried in the narrative body. Strip a
# leading level-1 heading from the narrative so it isn't duplicated under the
# emitted h1. Only a FIRST-line `# ...` (h1) is removed; the `## Subheading` chapter
# Chapter headings starting with `##` remain unchanged.
# `\n*` (not `\n?`) also consumes the blank line(s) after the title so the body
# starts at the first paragraph, not a stray leading newline.
_LEADING_H1_RE = re.compile(r"^\s*#[ \t]+[^\n]+\n*")


def _strip_leading_h1(content: str) -> str:
    m = _LEADING_H1_RE.match(content)
    return content[m.end() :] if m else content


def leading_h1_title(content: str) -> str | None:
    """正文首行 ``# 标题`` 的文字（与 `_strip_leading_h1` 同一正则），没有 ⇒ None。"""
    m = _LEADING_H1_RE.match(content or "")
    if not m:
        return None
    return m.group(0).strip().lstrip("#").strip() or None


def _data_system_label(tool: str) -> str | None:
    """Map a tool name to its data-system family (for the design's meta row).

    P2c (tool-domain): the dataset/kb/web/external families now derive from
    tool_category.lookup_domain (the source SSOT) so they stay in sync + team/MCP tools count
    as 外部. 'compute' is intentionally kept finer here than the source enum — only
    python_sandbox is a 计算 data system; canvas_/skill_ (also source='compute') are rendering
    tools, not data systems, so they stay unlabelled.
    """
    t = (tool or "").strip()
    if not t:
        return None
    from bowhead_core.tool_category import lookup_domain

    src = lookup_domain(t)
    label = {"dataset": "BI", "kb": "知识库", "web": "网络", "external": "外部"}.get(src)
    if label:
        return label
    if t == "python_sandbox":  # the only source='compute' tool that is a data system
        return "计算"
    return None


def _count_data_systems(tool_invocations: list[Any] | None) -> int:
    """Distinct data systems touched this turn (BI / KB / 网络 / 计算 / 外部 for team/MCP)."""
    systems: set[str] = set()
    for inv in tool_invocations or []:
        tool = getattr(inv, "tool_name", None)
        if tool is None and isinstance(inv, dict):
            tool = inv.get("tool_name") or inv.get("tool")
        lbl = _data_system_label(str(tool or ""))
        if lbl:
            systems.add(lbl)
    return len(systems)


def _report_meta_pills(
    payload: dict[str, Any],
    tool_invocations: list[Any] | None,
    lang: str = "en-US",
) -> list[str]:
    """The design's meta row pills: updated date · N data systems · Bowhead.
    (turns count isn't available at the compose chokepoint, so it's omitted.)
    chrome-i18n：文案走 report_chrome_text 表（dogfood 实锤 en 报告 pills 渲中文）。"""
    from bowhead_core.report_chrome_text import chrome_text

    pills: list[str] = []
    try:
        import datetime as dt

        pills.append(chrome_text("updated_on", lang, date=dt.date.today().isoformat()))
    except Exception:  # noqa: S110
        pass
    n_sys = _count_data_systems(tool_invocations)
    if n_sys == 1:
        pills.append(chrome_text("data_system_one", lang))
    elif n_sys:
        pills.append(chrome_text("data_systems", lang, n=n_sys))
    pills.append(chrome_text("generated_by", lang))
    return pills


async def _emit_document_chrome_head(
    canvas_store: Any,
    bus: Any,
    canvas_id: str,
    request: Any,
    payload: dict[str, Any],
    tool_invocations: list[Any] | None,
    skip: set[str] | None = None,
) -> list[str]:
    """外壳的「头」（h1 + 署名段）——composer 同一条骨架 + 投影（tech-design-chapter-split §2.6）。

    ``skip``：本轮已渲过的外壳件（``h1`` / ``byline``），**按件**跳过。不能按「有任意一件就整块跳」——
    eager 在 `payload.title` 为空时只发 meta（记作 byline）不发 h1，整块跳会让这份报告一个标题都没有
    （review 第 3 条），顺带还跳掉了本函数里的 `pin_turn_output_language` 副作用。"""
    from bowhead_core.canvas_doc_projection import document_chrome_sections
    from bowhead_core.report_chrome_text import chrome_lang

    turn_id = getattr(request, "turn_id", None)
    written: list[str] = []
    try:
        with contextlib.suppress(Exception):
            from bowhead_core.canvas_composer import payload_prose_for_language
            from bowhead_core.effective_language import carrier_language, language_decision_v2_enabled, pin_turn_output_language

            if language_decision_v2_enabled():
                _observed = carrier_language(payload_prose_for_language(payload))
                if _observed:
                    pin_turn_output_language(bus, _observed)
        lang = chrome_lang(getattr(request, "language_context", None))
        chrome = dict(payload)
        # Title fallback (tech-design-mixed-turn-chrome §2.3): if payload lacks title (peer crashed before subagent_return),
        # use the handwritten first-line `# Title` (write_report records it on the bus before persistence; bake strips it). Healthy turns
        # retain payload.title priority, byte-identically; still never fall back to the user's original wording.
        if not chrome.get("title"):
            from bowhead_core.canvas_revise import authored_title as _authored_title

            _fallback_title = _authored_title(bus)
            if _fallback_title:
                chrome["title"] = _fallback_title
        if chrome.get("title"):
            chrome["title"] = _truncate_heading(str(chrome["title"]))
        if chrome.get("eyebrow"):
            chrome["eyebrow"] = _truncate_heading(str(chrome["eyebrow"]))
        # Header needs only title / eyebrow; recommendations / caveats use the same helper in finalization tail.
        chrome.pop("recommendations", None)
        chrome.pop("caveats", None)
        head, _tail = document_chrome_sections(chrome, lang=lang, meta_pills=_report_meta_pills(payload, tool_invocations, lang))
        _skip = skip or set()
        for sec in head:
            kind = str(sec.get("kind"))
            body = {k: v for k, v in sec.items() if k != "kind"}
            piece = "byline" if body.get("style") == "byline" else kind
            if piece in _skip:
                continue
            await _emit_section(canvas_store, bus, canvas_id, turn_id, kind, body)
            written.append(piece)
        if not written and payload.get("title"):
            logger.warning("report_writer_loop.chrome_head_empty turn=%s", turn_id)
    except Exception:
        logger.exception("report_writer_loop.header_failed turn=%s", turn_id)
    return written


async def _emit_report_header(
    canvas_store: Any,
    bus: Any,
    canvas_id: str,
    request: Any,
    payload: dict[str, Any],
    tool_invocations: list[Any] | None,
    skip: set[str] | None = None,
) -> list[str]:
    """接设计稿 P1: emit the report's Verified-intelligence HEADER — eyebrow +
    meta row — above the body, so the (already-ported) rc-* CSS + TrustStrip
    render the design header. The writer-loop previously emitted only
    kpi_row+narrative+sources, dropping the eyebrow/meta the legacy card path had.
    Title stays the narrative's leading ``# 标题`` (rendered as the report h1).
    Best-effort: header is additive, never fatal to the compose."""
    # Canvas-elements document projection (same flag): writer chrome also avoids pills, folding eyebrow + meta
    # into one italic `style=byline` narrative after h1 (promote_document_chrome
    # moves it first with h1 / kpi_row). Composer integrates the same line at the introduction's start.

    return await _emit_document_chrome_head(canvas_store, bus, canvas_id, request, payload, tool_invocations, skip=skip)


# Criterion for significant numbers with units, forming `_collect_unverified`'s matching skeleton
# for gen3 structured unverified_numbers. Lookbehind uses only ASCII word characters (**not \w**: Python \w matches
# CJK and would reject almost every number in Chinese prose). Exclude identifier digits (art_123), already matched
# decimals and marker internals, while allowing adjacent CJK / whitespace / punctuation.
#
# citation-audit M3-2: this regex's original owner `_apply_number_provenance` (gen2 dual pass:
# unverified -> amber `[num:V:U]`, verified -> checked `[num:V:U:src:S]`) was deleted with reader retirement. Preserve
# its criterion **unchanged**: M1-3 structured replacement reuses it verbatim, changing output shape only.
# A minus sign must be **adjacent** (`-37.5` accepted, `- 37.5` rejected): the latter is a Markdown list marker;
# reading a bullet with 37.5% growth as -37.5 is wrong. `verify_numbers_in_text` already correctly recognizes
# `-37.5` as unauthorized, but token extraction here missed the negative form and silently dropped it.
# Table year-over-year / period-over-period / difference columns naturally contain many negatives; table verification makes this widespread
# rather than occasional prose. Dates remain unaffected: the digit before `-` in `2026-08-11` makes lookbehind exclude it.
#
# A hyphen following **CJK / % / right parenthesis** is a **range delimiter**, not a minus: `预计规模 1000万-1600万元`
# interpreted as `-1600万元` reports a fabricated negative (measured output `['1000万', '-1600万元']`).
# Import the criterion fragment from `number_verifier` **rather than duplicating it**: token extraction here and authorization there
# must split each string into identical numbers, or values silently disappear (see its comments).
# When the negative branch fails, restart unsigned matching at `1600`, correctly extracting both range endpoints;
# Chinese compound-number forms such as `3万5000元` remain unaffected.
_MARK_NUM_RE = re.compile(
    rf"(?<![A-Za-z0-9_.\[:])({MINUS_IF_NOT_RANGE}\d[\d,]*(?:\.\d+)?)"
    r"\s*(亿元|万元|亿|万|%|元|个|家|pp|倍)",
)


# Table criteria (separator / column splitting / header units) now live in `markdown_tables`, **shared by verification and binding**.
# Drift makes the same number visible to verification but invisible to binding, adding only to the denominator,
# systematically understating trust percentages. Do not duplicate it here; module docstring explains reasons and historical symptoms.
_TABLE_SEP_RE = _md_tables.TABLE_SEP_RE
_HEADER_UNITS = _md_tables.HEADER_UNITS
_HEADER_UNIT_RE = _md_tables.HEADER_UNIT_RE
_UNESCAPED_PIPE_RE = _md_tables.UNESCAPED_PIPE_RE
_BARE_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.\[:])(\d[\d,]*(?:\.\d+)?)")
# Maximum tokens contributed by one table: `_is_derivable` is O(n³) and runs synchronously on the event loop;
# a model-generated hundred-row fabricated table blocked for ~nine seconds. Stop verifying further table rows at the cap, but keep scanning prose.
_TABLE_TOKEN_BUDGET = 20


def _split_row(line: str) -> list[str]:
    """见 `markdown_tables.split_row` —— 与绑定侧共用同一份切列规则。"""
    return _md_tables.split_row(line)


def _header_units(header_line: str) -> list[str | None]:
    """见 `markdown_tables.header_units` —— 与绑定侧共用同一份表头单位规则。"""
    return _md_tables.header_units(header_line)


def _apply_header_units(row_line: str, col_units: list[str | None]) -> str:
    """给「单位写在表头」的裸数字补上单位，**仅供核验**，不改写落库内容。

    `_MARK_NUM_RE` 要求数字紧跟单位，而 BI 表格的常见形态是单位在表头、单元格里是
    `43,964` 这样的裸数字（116 全库：3,057 个，占表格数字的三分之二）。不补单位，
    这些数字既取不到 token 也没法做量级换算，等于漏检。

    补单位的判据是**逐个数字**看它后面紧跟的是不是单位，不是「整格有没有出现过单位字」：
    后者会让 `88888（+12%）` 这种复合单元格整格失去表头单位（`%` 触发守卫），
    也会漏掉同格里的第二个数字（`43,964（去年 99,999）` 的 99,999）——两者都是静默漏检。
    """
    if not col_units:
        return row_line
    cells = _split_row(row_line)
    out: list[str] = []
    for i, cell in enumerate(cells):
        unit = col_units[i] if i < len(col_units) else None
        if unit:

            def _suffix(m: re.Match[str], _u: str = unit, _cell: str = cell) -> str:
                tail = _cell[m.end() :].lstrip()
                if any(tail.startswith(u) for u in _HEADER_UNITS):
                    return m.group(0)  # This number already has a unit; do not add it twice.
                return f"{m.group(1)}{_u}"

            cell = _BARE_NUM_RE.sub(_suffix, cell)
        out.append(cell.strip())
    return "| " + " | ".join(out) + " |"


def _collect_unverified(content: str, authorized: set[float], *, trace: str = "") -> list[str]:
    """citation-audit M1-3: the structured replacement for the amber
    ``[num:V:U]`` wrap（只有带单位的显著数字才算, `_MARK_NUM_RE`),
    output = ``值+单位`` strings for ``payload.unverified_numbers``（FE 渲
    narrative banner; report_trust 计 unverified）。

    **表格行必须核验**（2026-08-10）：老实现是把琥珀标记 `[num:V:U]` **内联注入正文**，
    往表格单元格里注标记会破排版，所以跳过 `|` 行——那是一份**排版**清单。M1-3 之后
    输出改成结构化字段 `unverified_numbers`，不再往正文注入任何东西，**排版理由随之
    消失**，而跳过却留了下来，等于把排版清单当成了安全清单（与 citation-audit M3-6
    修过的 `_SKIP_LINE_PREFIXES` 是同一形状：极性相反的两份清单被混用）。

    后果实测（116 全库）：narrative 的 markdown 表格里共 4,658 个数字，
    `report_trust` 对其中 **0 个**做过核验——而 writer 的铁律 6 恰恰要求
    「对比用 markdown 表格」，BI 报告正文天然大量是表格。取消跳过后，其中
    **1,601 个自带单位**的数字进入核验；另外 3,057 个是裸数字（单位在表头），
    `_MARK_NUM_RE` 仍够不着，需要表头单位推断才能覆盖（未做，见 REPORT_WRITING.md）。

    标题/blockquote/figure 锚/code fence 仍跳过：它们本就不承载待核验的业务数字。
    """
    from bowhead_core.number_verifier import verify_numbers_in_text

    # Collect prose and tables separately, then concatenate **prose first**: tables often precede conclusions, so document-order truncation
    # pushes prospective fabrication, such as annual profit forecasts of 666.6 billion, out of the 20-item window despite needing scrutiny most.
    prose_out: list[str] = []
    table_out: list[str] = []
    seen: set[str] = set()
    in_fence = False
    lines = content.split("\n")
    # Current table's column units from headers, e.g. `营业额（万元）` -> `万元`. Clear at table end.
    col_units: list[str | None] = []
    # Current row index within this table: -1=outside, 0=header, 1=separator, >=2=data.
    # **Recognize table structure by position, not by rematching separator regex on every row**; the latter fails because
    # entirely empty/placeholder rows `| - | - |` / `| --- | --- |` are **syntactically identical** to separators.
    # The old row-wise matcher treats placeholders as separators, making the **preceding row** an unverified new header
    # and overwriting col_units with all None, stripping header units from every subsequent row.
    # Measurement: a middle `| --- | --- |` row made the old implementation return `[]` for the entire table.
    table_line_no = -1
    for idx, line in enumerate(lines):
        st = line.lstrip()
        if st.startswith("```"):
            in_fence = not in_fence
            col_units = []
            table_line_no = -1
            continue
        if in_fence or not st or st.startswith(("#", ">", "[figure:")):
            col_units = []
            table_line_no = -1
            continue
        in_table = st.startswith("|")
        if in_table:
            if table_line_no < 0:
                # First table row: recognize a header only if the next row is a separator; otherwise treat as unheaded loose `|` rows.
                nxt = lines[idx + 1].lstrip() if idx + 1 < len(lines) else ""
                if _TABLE_SEP_RE.match(nxt):
                    col_units = _header_units(st)
                    table_line_no = 1
                    continue
                col_units = []
                table_line_no = 2  # Scan as ordinary data rows, without seeking later headers.
            elif table_line_no == 1:
                # Separators contain no business numbers; the previous row already provided header units.
                table_line_no = 2
                continue
            if len(table_out) >= _TABLE_TOKEN_BUDGET:
                # Table token cap reached: skip verification of subsequent table rows (`_is_derivable` is O(n³), synchronous,
                # blocking ~nine seconds on a hundred-row fabricated table), but **keep scanning prose**.
                continue
            # Data rows: append header units to bare values, then verify value+unit.
            line = _apply_header_units(st, col_units)
        else:
            col_units = []
            table_line_no = -1
        unauth = verify_numbers_in_text(line, authorized, trace=trace)
        if not unauth:
            continue
        norm = {raw.replace(",", "") for raw, _v in unauth}
        for m in _MARK_NUM_RE.finditer(line):
            num, unit = m.group(1), m.group(2)
            if num.replace(",", "") not in norm:
                continue
            token = f"{num}{unit}"
            if token in seen:
                continue
            seen.add(token)
            (table_out if in_table else prose_out).append(token)
    return (prose_out + table_out)[:_UNVERIFIED_LIST_CAP]


#: Maximum entries in structured `unverified_numbers` (frontend renders only this many; more are unreadable).
#: ⚠ This is a **display** cap, not a counting cap. Trust's denominator must use the actual total
#: (`unverified_total`), or truncation raises apparent trust;
#: an optimistically misleading trust measure is worse than no measure.
_UNVERIFIED_LIST_CAP = 20


def _apply_citation_pass(
    content: str,
    tool_invocations: list[Any] | None,
    reg: Any,
    *,
    trace: str = "",
    bind: bool = True,
    stats_out: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """citation-audit M1-2/M1-3 — the gen3 replacement for
    ``_apply_number_provenance``. Deterministic, single wire format:

      1. bind — single-source-matched numbers → ``值[+单位] [N]`` against the
         turn registry (binding conditions reused verbatim from the card-path
         binder via ``citation_marks.bind_to_numbered``; lazy registry adds
         flow into the SAME Sources section materialised after the loop).
      2. flag — numbers not backed by any tool result → structured
         ``unverified_numbers`` (NO inline amber marker).

    ``bind=False``（canvas-update-turn 批 B0：跨轮改段）只做第 2 步——本轮编号不能落进上一轮的段。
    No tool data → nothing to bind or flag (same posture as the old pass).
    Best-effort: any failure returns the original content (marking never
    fatal)."""
    try:
        from bowhead_core import citation_marks
        from bowhead_core.canvas_composers.answer_decomposer import (
            _build_value_to_source_map,
        )
        from bowhead_core.number_verifier import build_authorized_numbers

        invs = tool_invocations or []
        authorized = build_authorized_numbers(invs)
        if stats_out is not None:
            # Both no tool data and all values verified return []; callers rewriting cross-turn sections must distinguish them.
            stats_out["authorized"] = bool(authorized)
        if not authorized:
            return content, []
        unverified = _collect_unverified(content, authorized, trace=trace)
        value_map = _build_value_to_source_map(invs) if bind else None
        bound = content
        if value_map:
            bound = citation_marks.bind_to_numbered(
                content,
                value_to_source=value_map,
                sources=reg.sources,
                id_to_index=reg.id_to_index,
                tool_invocations=invs,
            )
        # W1-A observability (renamed for changed semantics): unverified numbers.
        # `report_numbered_bound` was emitted here before M3 moved it inside `bind_to_numbered`, the sole binding
        # seam; otherwise the card path's three binding points were invisible (implementation review deletion-safety P2:
        # bind-rate signal vanished on generation change). Emitting again here would double-count.
        try:
            from bowhead_core.metrics import observe as _obs

            _buckets = (0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0)
            _obs(
                "report_num_flagged_unverified",
                float(len(unverified)),
                buckets=_buckets,
            )
        except Exception:  # noqa: S110
            pass
        return bound, unverified
    except Exception:
        logger.exception("report_writer_loop.citation_pass_failed")
        return content, []


async def _cross_turn_bind_ok(ctx: Any) -> bool:
    """跨轮改段时 registry 是否是**画布作用域**（可铸文档编号）。取不到画布 / merge flag 关 / 画布已分叉 ⇒ False。"""
    try:
        from bowhead_core.sources_resolver import find_canvas_sources_section

        cvs = getattr(ctx, "canvas_store", None)
        sid = getattr(ctx, "session_id", None)
        if cvs is None or not sid:
            return False
        cv = await cvs.get_canvas_by_session(sid)
        if cv is None:
            return False
        _existing, canvas_scope = find_canvas_sources_section(await cvs.list_sections(cv.id), turn_id=getattr(ctx, "turn_id", None))
        return bool(canvas_scope)
    except Exception:
        return False


# Knowledge-base citations batch 3: replace prose `[ref:<chunk_id>]` with placeholders before strip_legacy
# (write_report already translated `[ref:N]` using this run's table). Nonnumeric placeholders avoid false antifabrication warnings; dynamically register
# actual current-turn kb_search chunks as sources [N]. Delete fabricated chunks or unnumberable ones (cross-turn scope/unavailable registry) as before.
_KB_REF_IN_REPORT_RE = re.compile(r"\s?\[ref:(chunk_[A-Za-z0-9]+)\]")
_KB_REF_PLACEHOLDER = "\ue000bowhead-kb-ref\ue001"

# Model-authored numbers: static stage creates placeholders; dynamic stage decides whether to translate into real numbers or strip.
# `num` = bare [N]; `cite` = [cite:N] outside the writer loop (only that loop's prompt lists source numbers;
# other agents see only ν1.5 list numbering, so their [cite:N] necessarily refers to that list).
_MODEL_NUMBER_RE = re.compile("\ue000bowhead-(num|cite):(\\d{1,3})\ue001")


def _model_number_placeholder(n: int) -> str:
    return f"\ue000bowhead-num:{n}\ue001"


def _model_cite_placeholder(n: int) -> str:
    return f"\ue000bowhead-cite:{n}\ue001"


def _translate_model_numbers(md: str, reg: Any, invs: list[Any], *, trace: str = "") -> str:
    """把模型写的号按**本 run 自己那份** ν1.5 表翻成来源编号，翻不了的剥掉。

    模型看到的数据清单是会话级简单整数（`<data id="127" …/>`），报告正文又满是 `[26]` 这种来源号，
    它就照样子把数据编号写成 `[127]`。裸 [N] 只在号**大于登记表当前条数**时才当清单编号翻：不超过条数的
    号可能是它从读到的正文里抄的来源号，翻了会绑到错的数据上（错绑比丢一个引用更坏），照剥。
    非写手写的 [cite:N] 不受这条限：正文里存的从来不是 `[cite:…]`，抄不来，只能是清单号
    （116 `t_418272316b73`：裸 `[24]` 被剥后模型改写 `[cite:24]`，原样按来源号收下、挂到了「各月客流量」上）。
    只翻 kind=data 且本轮登记表或工具调用里认得的数据；图/文档的号各有自己的写法，不走这里。
    """
    if _MODEL_NUMBER_RE.search(md) is None:
        return md
    from bowhead_core.agents._lead_context import get_current_ref_allowlist
    from bowhead_core.sources_resolver import _alloc_source_index, _build_inv_lookup

    allowlist = get_current_ref_allowlist()
    size = reg.size()
    lookup = _build_inv_lookup(invs)
    counts = {"translated": 0, "stripped": 0}

    def _sub(m: re.Match[str]) -> str:
        n = int(m.group(2))
        listed = m.group(1) == "cite" or n > size
        real = allowlist.resolve(str(n)) if allowlist is not None and listed and allowlist.kind_of(n) == "data" else None
        if not real or (real not in reg.id_to_index and real not in lookup):
            counts["stripped"] += 1
            return ""
        counts["translated"] += 1
        return f"[{_alloc_source_index(real, sources=reg.sources, id_to_index=reg.id_to_index, inv_lookup=lookup)}]"

    md = _MODEL_NUMBER_RE.sub(_sub, md)
    logger.info("bake.model_numbers turn=%s translated=%d stripped=%d", trace, counts["translated"], counts["stripped"])
    return md


def _mask_kb_refs(md: str) -> tuple[str, list[str]]:
    chunk_ids: list[str] = []

    def _mask(m: re.Match[str]) -> str:
        chunk_ids.append(m.group(1))
        return _KB_REF_PLACEHOLDER

    return _KB_REF_IN_REPORT_RE.sub(_mask, md), chunk_ids


def _bind_kb_refs(md: str, chunk_ids: list[str], reg: Any, invs: list[Any] | None) -> str:
    """占位 → [N]（按本轮 kb_search 结果登记进 turn registry，label=文件名）；reg 为 None ⇒ 全删。"""
    if not chunk_ids:
        return md
    index: dict[str, tuple[str, str]] = {}
    if reg is not None:
        from bowhead_core.agents._lead_context import parent_kb_invocations
        from bowhead_core.kb_citations import kb_chunk_index

        # This run's retrieval + parent-run retrieval (common shape: Lead searches, then delegates report writing to a peer).
        index = kb_chunk_index([*parent_kb_invocations(), *(invs or [])])
    it = iter(chunk_ids)

    def _sub(_m: re.Match[str]) -> str:
        chunk_id = next(it, None)
        hit = index.get(chunk_id) if chunk_id else None
        if hit is None:
            return ""
        _file_id, file_name = hit
        n = reg.register_source(chunk_id, label=file_name or None, tool="kb_search")
        return f" [{n}]"

    return re.sub(re.escape(_KB_REF_PLACEHOLDER), _sub, md)


async def bake_report_markdown(
    content: str,
    *,
    ctx: Any,
    figure_ids: set[str] | None = None,
    is_update: bool = False,
    trace: str = "",
    cross_turn: bool = False,
    stats_out: dict[str, Any] | None = None,
    prior_markdown: str | None = None,
) -> tuple[str, list[str]]:
    """delivery-surface M-B —— 卫兵下沉进笔：write_report 写路的统一溯源/防伪管线。

    ``cross_turn``（canvas-update-turn 批 B0 review 修 / 批 B1-5）：就地改**上一轮**的段。读侧按画布第一个
    来源段解析 [N]。来源段是**画布作用域**（canvas-elements 来源跨轮合并，registry 已从画布级来源段回灌）
    时新号就是文档编号，照铸；turn 作用域（merge flag 关 / 画布已分叉）时本轮编号会落进上一轮的段、标错
    来源，此时 ``[cite:N]`` 剥掉、不绑 [N]、不换写裸 id（`_cross_turn_bind_ok`）。旧正文里的 [N] 照
    ``is_update`` 保留；数字防伪照跑（unverified 仍算）。

    此前这套管线（strip_legacy / strip_untrusted / [cite:N] 解析 / [N] 铸造+数字
    防伪 / 裸 artifact id 换写 / figure 锚校验）长在**写手循环**的 post-LLM 缝里
    （tool-loop 与 _direct_compose 各一份）——把笔递给任何别的 agent，卫兵就全丢。
    下沉到写路后任何持笔者（写手循环 / peer / 直调）一视同仁，也顺带消掉两份
    实现的漂移面。

    输入取处（tech-design §13.4.4 F8 表）：
    - registry: ``get_turn_sources_registry(bus, ...)``，bus = ctx.parent_bus/ctx.bus
      —— 与写手循环/composer 同一个 turn 级编号空间（M1-1 不变量）。
    - authorized 数字的 invocations: ``ctx._report_bake_invocations``（写手循环
      在 ctx 上显式挂）或 ``ctx._current_subagent_invocations``（peer 持笔时
      subagent loop 已维护）。
    - ``is_update``: 显式 section_id 更新是回读-改写回合，旧正文里的 [N] 是合法
      引用 → strip_untrusted 豁免（impl review C-P1-1/A-P1-2 实证，语义原样搬）。

    返回 (baked_md, unverified_numbers)。best-effort：citation pass 内部自兜底，
    本函数不抛（正则/纯函数级别）。
    """
    from bowhead_core import citation_marks as _cm

    # ── Static stage (pure regex, no external dependencies): **runs on every path**. Review R5-10: the old handler
    # wrapped all bake in one try, so registry errors failed open for strip_untrusted/figure anchors too,
    # persisting model-fabricated bare [N] as trusted citations and triggering hard rejection for invalid figure anchors.
    md, _kb_chunk_ids = _mask_kb_refs(content or "")
    md = _cm.strip_legacy(md)
    # Model-authored bare [N] is untrusted; only downstream mints legitimate numbers. Updates previously exempted everything because old prose had valid citations,
    # also persisting newly invented numbers. 116 `s_18c8b86f3003` revision used ν1.5 data-list numbers as citations
    # `[127]`/`[130]` with only 62 sources, leaving citations inaccessible and mismatched to artifacts. Updates now allow only numbers
    # already present in old prose; new numbers become placeholders until `_translate_model_numbers` can translate them after registry readiness.
    # Without old prose (`prior_markdown is None`), preserve the whole-section exemption to avoid stripping legitimate citations.
    if not is_update or prior_markdown is not None:
        _keep = frozenset(int(x) for x in _cm.NUMBERED_RE.findall(prior_markdown or "")) if is_update else frozenset()
        md = _cm.strip_untrusted_numbered(md, keep=_keep, mask=_model_number_placeholder)
    if not getattr(ctx, "_cites_are_source_numbers", False):
        md = _CITE_RE.sub(lambda m: _model_cite_placeholder(int(m.group(1))) if m.group(1).isdigit() else m.group(0), md)
    md = _strip_leading_h1(md)
    if figure_ids is not None:
        # canvas-update-turn batch B2-4 (tech-design-b2 §3.6 step 1): since 09-15, 75% of charts lacked prose anchors; previously
        # stripped counts were discarded and tool arguments unpersisted, concealing whether anchors were unwritten or stripped. Log each write
        # using written/stripped counts; record numbers only, never prose.
        _written = len(_FIGURE_ID_IN_RUN.findall(md))
        # 116 dogfood (1.3.43, `t_a855…`/`t_d4765c0ccb5c`): written=2 stripped=2 valid=0 figure_pool=2; charts existed but all anchors were stripped.
        # Counts cannot identify model-written IDs (art_ data IDs? invented IDs?). Log invalid IDs themselves, at most five, without prose.
        _invalid_ids = [fid for fid in _FIGURE_ID_IN_RUN.findall(_FENCED_BLOCK_RE.sub("", md)) if fid not in figure_ids][:5]
        md, _stripped = _strip_invalid_figure_anchors(md, figure_ids)
        # Review P2: written−stripped incorrectly includes fenced anchors and invalid inline anchors outside stripping scope;
        # count actual-chart anchors remaining outside fences after stripping instead.
        _valid = sum(1 for fid in _FIGURE_ID_IN_RUN.findall(_FENCED_BLOCK_RE.sub("", md)) if fid in figure_ids)
        if stats_out is not None:
            stats_out["figure_anchors_written"] = _written
            stats_out["figure_anchors_stripped"] = _stripped
            stats_out["figure_anchors_valid"] = _valid
        logger.info(
            "write_report.figure_anchors turn=%s written=%d stripped=%d valid=%d figure_pool=%d update=%s invalid_ids=%s",
            trace,
            _written,
            _stripped,
            _valid,
            len(figure_ids),
            is_update,
            _invalid_ids,
        )

    # ── Dynamic stage (registry/numeric antifabrication): handles its own fallback; failures degrade only this stage.
    unv: list[str] = []
    try:
        bus = getattr(ctx, "parent_bus", None) or getattr(ctx, "bus", None)
        # Review R5-9: outside the writer loop (peer/direct calls), registry is empty on fresh/resume buses.
        # Centralize construction and writeback in **one entry** (canvas-elements source merge: only three of six former construction sites
        # wrote back, already breaking design v1). Best effort; failures proceed with an empty registry.
        reg = (
            await get_turn_sources_registry(
                bus,
                canvas_store=getattr(ctx, "canvas_store", None),
                session_id=getattr(ctx, "session_id", None),
                turn_id=getattr(ctx, "turn_id", None),
            )
            if bus is not None
            else None
        )
        if reg is not None:
            # Batch B1-5: cross-turn section edits may mint [N] depending on sources **scope**. Under canvas scope (canvas-elements cross-turn source merge,
            # enabled by default), registry rehydrates from canvas sources and readers resolve against the first sources section: new document numbers are valid.
            # Under turn scope (flag disabled / forked canvas), current numbers would enter the previous turn's section: still strip [cite:N] without binding (B0).
            bind = True
            if cross_turn:
                bind = await _cross_turn_bind_ok(ctx)
                logger.info("bake.cross_turn_bind turn=%s scope=%s", trace, "canvas" if bind else "turn")
            md = _resolve_cites(md, reg.size() if bind else 0)
            # Review R5-8: isolate parallel siblings with task-local ContextVar priority
            # (same B-P2-9 source as canvas_tools); shared attributes serve only legacy harness fallback.
            # Test absence with `is not None`; explicit empty lists must not disappear in an or-chain.
            from bowhead_core.agents._lead_context import (
                get_current_subagent_invocations,
            )

            invs = getattr(ctx, "_report_bake_invocations", None)
            if invs is None:
                invs = get_current_subagent_invocations()
            if invs is None:
                invs = getattr(ctx, "_current_subagent_invocations", None)
            invs = list(invs or [])
            # Translate before binding: translated [N] sits beside its number, so the binder's adjacent-citation guard prevents a second number.
            # Even when not minting, strip before binding/antifabrication: placeholder digits could otherwise be read as prose numbers.
            md = _translate_model_numbers(md, reg, invs, trace=trace) if bind else _MODEL_NUMBER_RE.sub("", md)
            md, unv = _apply_citation_pass(md, invs, reg, trace=trace, bind=bind, stats_out=stats_out)
            if bind:
                with contextlib.suppress(Exception):
                    md = reg.resolve_bare_ids(md, invs)
            md = _bind_kb_refs(md, _kb_chunk_ids, reg if bind else None, invs)
        else:
            md = _resolve_cites(md, 0)
    except Exception:
        logger.exception("bake_report_markdown.dynamic_stage_failed trace=%s — 静态卫兵已生效, [N]/防伪降级", trace)
        md = _resolve_cites(md, 0)
    if _KB_REF_PLACEHOLDER in md:  # Registration not reached (no registry / dynamic-stage exception) -> delete as before.
        md = md.replace(_KB_REF_PLACEHOLDER, "")
    md = _MODEL_NUMBER_RE.sub("", md)  # Strip every untranslated model number, including cross-turn no-mint paths and dynamic-stage exceptions.
    return md, unv


async def _emit_kpi_band(
    canvas_store: Any,
    bus: Any,
    canvas_id: str,
    request: Any,
    payload: dict[str, Any],
) -> list[str]:
    """确定性 KPI 带（从单写手路原地抽出, review R5-4——finisher 也要发）。
    设计语义原样：delta 加方向符、sentiment 定色、sub/unit 不着色。无 kpis → []。"""
    written: list[str] = []
    try:
        from bowhead_core.canvas_revise import build_kpi_cells as _build_kpi_cells

        kpi_cells: list[dict[str, Any]] = _build_kpi_cells(payload)  # Batch B1-0: share this construction with in-place chrome patching.
        if kpi_cells:
            await _emit_section(
                canvas_store,
                bus,
                canvas_id,
                getattr(request, "turn_id", None),
                "kpi_row",
                {"cells": kpi_cells[:8]},
            )
            written.append("kpi_row")
    except Exception:
        logger.exception(
            "report_writer_loop.kpi_band_failed turn=%s",
            getattr(request, "turn_id", None),
        )
    return written


async def _promote_document_chrome_best_effort(canvas_store: Any, bus: Any, request: Any) -> None:
    """把本轮的文档级 header chrome 提到画布最前（批 2-c0；review B2-3 后收敛到共用）。

    **compose_report 的三条出口都要调**：亲笔收尾器 / 缺省代笔卡片 / 散文写手 loop。
    ★ review B2-3 逮到的洞：原本只装在亲笔收尾器里。卡片路直接调
    `compose_canvas_from_payload`，而批 1-b 在「本轮产出腿 N≥2」时会压掉 promote_to_top
    ⇒ 末轮 chrome 纯 append 落在最末、没有任何东西把它提前。本 sprint 的现场恰好有 2 条腿
    亲笔（走了收尾器），所以第一轮实现没暴露它。

    best-effort：排序失败不能把一次已经写成的报告带崩（同 `_apply_canonical_order`）。
    """
    turn_id = getattr(request, "turn_id", None)
    try:
        from bowhead_core.canvas_composer import promote_document_chrome as _promote_chrome

        canvas = await canvas_store.get_canvas_by_session(getattr(request, "session_id", "") or "")
        if canvas is None:
            return
        await _promote_chrome(canvas_store=canvas_store, bus=bus, turn_id=turn_id, canvas_id=canvas.id)
    except Exception:
        logger.warning("compose_report.chrome_promote_failed turn=%s", turn_id, exc_info=True)


async def _apply_canonical_order(canvas_store: Any, bus: Any, canvas_id: str, turn_id: str | None) -> None:
    """writer-loop 收尾时把画布重排成**展示规范序**（header 领跑 → body → code 附录沉底）。

    116 prod 1.1.28 dogfood 实测：最近 25 个画布里，**11 个（有 h1 的 22 个里的一半）标题被
    正文压在后面**，形状每次都一样：`code_block* → chart* → narrative → h1`。用户看到的报告，
    标题在正文中间。

    根因：`canvas_rank` 的模块注释写得清清楚楚——这份 rank 存在的理由就是
    「writer-loop 在 compose 期才 emit 头部(eyebrow/h1/meta)，seq 晚于 peer-loop 期落库的
    code_block / 裸 chart，不重排则 python 代码块领跑报告」。**但它点名的那条路一次都没调它**：
    `canonical_order` 只接在确定性 composer（`canvas_composer.py:212`）上，writer-loop 侧全无。
    而 `FEATURE_REPORT_EDIT_SYNC_V2=1`（116 已开）让前端和导出器**信任库里的 seq**，
    于是错序被原样渲染、原样导出。

    与确定性 composer 同款处置：无条件套 rank（那边也没有 flag 判据）、稳定排序保住同 rank 内
    的相对序（`narrative→[figure:]` 的织入序、多写手章序不受影响）、无变化则 no-op 不发事件。
    best-effort：排序失败不能把一次已经写成的报告带崩。
    """
    try:
        if await canvas_store.has_manual_reorder(canvas_id):
            # User reordered this document -> canonical ordering yields. `FEATURE_REPORT_EDIT_SYNC_V2` means
            # persisted seq is the only authoritative order, **changed by user dragging**; the server must not undo it.
            logger.info("report_writer_loop.reorder_skip turn=%s reason=manual_order", turn_id)
            return
        sections = await canvas_store.list_sections(canvas_id)
        # Exclude folded old reports from sorting: `list_sections` retains superseded sections, while `_segment_bounds`
        # counts **all** h1; old h1 creates a boundary stopping current code blocks early and partly defeating bottom placement.
        # 116 uses `BOWHEAD_CANVAS_LATEST_WINS=1`; `supersede_stale_owner_sections` has no flag at all,
        # and folded sections already exist on today's canvases.
        # ⚠ Still **pass the complete order**: `reorder_sections` places listed items first and unlisted ones last
        # (`canvas_store.py` completes and renumbers). Passing only visible sections moves folded history **after** the current report.
        # Keep folded sections **in place**, filling original visible slots with canonically ordered visible sections.
        visible = [s for s in sections if not getattr(s, "superseded", False)]
        if not visible:
            return
        ordered_visible = iter(canonical_order(visible))
        new_order = [s.id if getattr(s, "superseded", False) else next(ordered_visible) for s in sections]
        if new_order == [s.id for s in sections]:
            return
        await canvas_store.reorder_sections(canvas_id=canvas_id, ordered_ids=new_order, by_turn_id=turn_id or MACHINE_REORDER_TURN_ID)
        await bus.emit(_event(turn_id, "canvas.reordered", {"canvas_id": canvas_id, "order": new_order}))
        logger.info("report_writer_loop.reordered turn=%s n=%d", turn_id, len(new_order))
    except Exception:
        logger.warning("report_writer_loop.reorder_failed turn=%s", turn_id, exc_info=True)


# Chunk throttling: one Chinese character ≈ one token; unthrottled output floods at token granularity (canvas_composers/
# streaming.py's 0.27.1 hotfix recorded 167 chunks / two sections in prod traces, stalling frontend).
# That file is marked for deletion next version, so keep a local implementation without depending on it.


async def _maybe_finish_peer_authored_report(
    *,
    canvas_store: Any,
    request: Any,
    bus: Any,
    payload: dict[str, Any],
    tool_invocations: list[Any] | None,
    artifact_store: Any = None,
    authored_owners_out: set[str] | None = None,
) -> list[str] | None:
    """delivery-surface M-D 报告收尾器（r2/F7）——peer 已自行文时的确定性收尾，零 LLM。

    检测：本轮 append 的 narrative section 且 owner 是 per-spawn id（``sub_`` 前缀，
    `report_tools._resolve_writer_id` 服务端盖章，LLM 伪造不了）。写手家族的 owner
    是 lead / report-editor / writer-*，不会误判。
    命中 → 补发 header(eyebrow/h1/meta) + KPI 带 + Sources（共享 registry，与
    peer 写路 bake 铸的 [N] 同一编号空间）并返回 kinds（含每段 peer narrative——
    下游 `_content_kinds` /「报告已生成」口径按真实内容算）。KPI 带（review #4）：
    单代笔路有确定性带、亲笔路原来没有——用聚合 payload 发同一条 `_emit_kpi_band`，
    kpi_row rank=3 归 header 区，晚 append 也会被 canonical rank 排到正文前。
    未命中 → None（走缺省代笔）。防双发：bus._md_finisher_done。
    ``authored_owners_out``：回传亲笔 owner 集（caller 算混合轮余量用）。
    """
    turn_id = getattr(request, "turn_id", None)
    try:
        if getattr(bus, "_md_finisher_done", False):
            return []
        # canvas-update-turn batch A2: centralize handwritten detection in canvas_revise.is_peer_authored_narrative
        # (same source as lead_orchestrator._peer_authored_narrative_exists). Eager **nonchapter**
        # cards are not handwritten because full chrome already rendered; finalizing again doubles h1/KPI in one turn (spike S5b).
        # Eager **chapters** (plan chapter_mode) still qualify, receiving document-level chrome here.
        from bowhead_core.canvas_revise import is_peer_authored_narrative, rendered_document_pool_text, turn_chrome_present

        secs = await canvas_store.list_sections_appended_by_turn(turn_id)
        peer_narr = [s for s in secs if is_peer_authored_narrative(s)]
        # Canvas-elements render-document-shell-once-per-turn (tech-design-mixed-turn-chrome §2.2): already rendered
        # **document-level** shell components (full eager nonchapter set / unowned sections). In mixed turns, payload arrives before write_report,
        # so eager cannot detect future output (ledger still empty); skip existing components individually from actual canvas state here. 116 A3/C5 had
        # h1×2, KPI×2 and recommendations×3. Exclude plan/handwritten chapter h1, since this still supplies the cover.
        _present = turn_chrome_present(secs)
        # Recommendations / caveats are **content pools**, not singletons: compare rendered text item by item and add only missing entries (review item 1).
        _pool_text = rendered_document_pool_text(secs)
    except Exception:
        logger.exception("compose_report.finisher_detect_failed turn=%s", turn_id)
        return None  # Probe failure -> default ghostwriting continues, failing open to previous behavior.
    if not peer_narr:
        return None
    if _present:
        logger.info("compose_report.finisher_skip_present turn=%s present=%s", turn_id, sorted(_present))
    if authored_owners_out is not None:
        authored_owners_out.update(str(getattr(s, "owner", "") or "") for s in peer_narr)
    with contextlib.suppress(Exception):
        bus._md_finisher_done = True
    written: list[str] = ["narrative"] * len(peer_narr)
    try:
        canvas = await canvas_store.get_canvas_by_session(request.session_id)
        if canvas is None:
            return written
        # canvas-update-turn batch B0: adding chapters (Lead spawn canvas_mode=append) emits no header/KPI band;
        # otherwise adding one section creates another h1 report, and A1 folds the old report (review P1-3).
        from bowhead_core.canvas_revise import lead_canvas_mode as _lead_mode
        from bowhead_core.canvas_revise import section_revise_enabled as _revise_on

        # Skip revise mode too: if compute ignores patch instructions and appends a whole new report, revision finalization misses it
        # and reaches here. Another h1/KPI creates a new document, folding the old one under 116 LATEST_WINS=1 (review P2-6).
        # Review fix (plan perspective): do not skip plan execution; only this supplies plan covers. A same-turn append/revise delegation
        # before plan execution leaves its mode on the bus, which must not be mistaken for the plan document's mode.
        if _revise_on() and _lead_mode(bus) in ("append", "revise") and not getattr(bus, "_d6_is_plan_execution", False):
            logger.info("compose_report.%s_mode_skip_chrome turn=%s", _lead_mode(bus), turn_id)
            # Batch B1-1 (design §2 final item): append turns leave title/KPI alone but refresh the updated-time pill;
            # the document was changed today. `touch_meta` is separate; other `patch_document_chrome` branches
            # skip without payload keys. (Document minimalism retired meta sections; this is a no-op for new reports.)
            with contextlib.suppress(Exception):
                from bowhead_core.canvas_revise import latest_document_block as _latest_block
                from bowhead_core.canvas_revise import patch_document_chrome as _patch_chrome_append
                from bowhead_core.report_chrome_text import chrome_lang as _append_lang

                _blk = _latest_block(await canvas_store.list_sections(canvas.id))
                if _blk is not None:
                    await _patch_chrome_append(
                        canvas_store,
                        bus,
                        canvas_id=canvas.id,
                        turn_id=turn_id,
                        block=_blk,
                        payload={},  # Use only the meta branch.
                        lang=_append_lang(getattr(request, "language_context", None)),
                    )
        else:
            # h1 and byline are separate: eager emits only meta (counted as byline), no h1, when title is empty.
            # Combining their checks leaves reports untitled (review item 3). Pass individual components so the emitter skips independently.
            written += await _emit_report_header(
                canvas_store, bus, canvas.id, request, payload or {}, tool_invocations, skip=_present & {"h1", "byline"}
            )
            if "kpi_row" not in _present:
                written += await _emit_kpi_band(canvas_store, bus, canvas.id, request, payload or {})
        reg = await get_turn_sources_registry(bus, canvas_store=canvas_store, canvas_id=canvas.id, turn_id=turn_id)
        reg.ingest_payload_sources((payload or {}).get("sources"), tool_invocations)
        reg.ingest_tool_invocations(tool_invocations)
        if not reg.is_empty():
            from bowhead_core.sources_resolver import enrich_source_labels

            _entries = await enrich_source_labels(reg.sources, artifact_store, user_id=getattr(request, "user_id", None))
            from bowhead_core.report_chrome_text import chrome_lang as _chrome_lang_f

            _sid = await upsert_turn_sources_section(
                canvas_store=canvas_store,
                bus=bus,
                canvas_id=canvas.id,
                turn_id=turn_id,
                entries=_entries,
                lang=_chrome_lang_f(getattr(request, "language_context", None)),
            )
            if _sid:
                written.append("sources")
        # Review #4 (FIX-2 parity): payload caveats (including plan missing-leg gap caveats,
        # added by Lead only after all legs finish, invisible to peers while writing) and provenance findings
        # both require deterministic sections; both replaced paths (writer prose + deterministic provenance /
        # card narrative-caveats) originally emitted them.
        #
        # Canvas-elements tech-design-document-minimalism §2.2 / §2.3 (user decision 2026-09-07):
        # **The system no longer renders recommendations or model-authored caveats as sections**. They are author-owned content; prompts teach prose inclusion
        # (50% of 312 handwritten reports already included `## 建议`, making system rendering duplicate them;
        # 12.8% had both sets. The same batch removed hard rejection for missing recommendations, eliminating 100 rejections over 180 days).
        # Caveat removal is a **tradeoff**, not deduplication: only 1.9% of authors include them, so they disappear from most reports
        # as a direct, user-accepted consequence. Keep `payload.recommendations` / `caveats` fields
        # as structured Lead evidence, without automatically creating canvas sections.
        #
        # **Continue rendering guard provenance warnings** below: the machine finds numbers without tool evidence,
        # which models cannot write themselves; deleting warnings removes the final fabrication gate.
        # §2.3 third class: plan missing-leg notices, like guard accusations, are **system-generated and unavailable to authors**
        # because legs have not finished during writing. In the FIX-2 / 2026-06-25 production incident, reports
        # looked complete when a leg failed, so this section cannot retire with author caveats.
        from bowhead_core.analysis_payload import plan_gap_caveats as _gap_cav
        from bowhead_core.analysis_payload import strip_caveat_marker as _strip_marker
        from bowhead_core.report_chrome_text import chrome_lang as _gap_lang
        from bowhead_core.report_chrome_text import chrome_text as _gap_text

        _gaps = [_strip_marker(c) for c in _gap_cav(payload or {}) if _strip_marker(c).strip() not in _pool_text]
        if _gaps:
            await _emit_section(
                canvas_store,
                bus,
                canvas.id,
                turn_id,
                "narrative",
                {
                    "markdown": _gap_text("caveats_heading", _gap_lang(getattr(request, "language_context", None)))
                    + "\n\n"
                    + "\n".join(f"- {c}" for c in _gaps),
                    "style": "caveats",
                    # Batch B1-1: mark system-generated so later chrome patches rewriting author caveats cannot overwrite it
                    # (`canvas_revise.is_system_caveat_section`; `_meta.gap` is a protected subkey).
                    "_meta": {"gap": True},
                },
            )
            written.append("caveats")

        from bowhead_core.analysis_payload import provenance_caveats

        # Provenance entries are machine findings models will not write: **never skip the whole block**; deduplicate only item by item.
        _prov = [c for c in provenance_caveats(payload or {}) if c.strip() not in _pool_text]
        from bowhead_core.report_chrome_text import chrome_lang as _pv_lang2
        from bowhead_core.report_chrome_text import chrome_text as _pv_text2

        _prov_heading2 = _pv_text2("provenance_heading", _pv_lang2(getattr(request, "language_context", None)))
        from bowhead_core.canvas_revise import no_document_chrome as _no_chrome_prov

        if _prov and _no_chrome_prov(bus):
            # Batch B1-5 (116 dogfood R10 `s_0a9c3b144419`): an append turn produced a **second** provenance-warning section.
            # Revision finalization already merged them, but handwritten finalization, mandatory for append, missed it. Merge into the document's provenance by line.
            from bowhead_core.canvas_revise import upsert_provenance_section as _upsert_prov_f

            if await _upsert_prov_f(
                canvas_store, bus, canvas_id=canvas.id, turn_id=turn_id, anchor_id=None, heading=_prov_heading2, lines=list(_prov)
            ):
                written.append("provenance_caveats")
        elif _prov:
            await _emit_section(
                canvas_store,
                bus,
                canvas.id,
                turn_id,
                "narrative",
                {"markdown": _prov_heading2 + "\n\n" + "\n".join(f"- {c}" for c in _prov), "style": "caveats", "_meta": {"provenance": True}},
            )
            written.append("provenance_caveats")
        # Batch B1-4: append/revise inserts current prose before document tail sections (canvas-level merged sources require no new tail area).
        from bowhead_core.canvas_revise import no_document_chrome as _no_chrome_place
        from bowhead_core.canvas_revise import place_appended_before_tail as _place_f

        if _no_chrome_place(bus):
            _moved = await _place_f(canvas_store, bus, canvas_id=canvas.id, turn_id=turn_id)
            if _moved:
                logger.info("compose_report.append_place turn=%s moved=%d via=finisher", turn_id, _moved)
    except Exception:
        # Probe succeeded: actual peer content exists; never invoke ghostwriting to author it again.
        # Missing some header/sources is preferable to duplicate narrative.
        logger.exception("compose_report.finisher_failed turn=%s", turn_id)
    # Batch 2-c0 chrome promotion moved into the thin `compose_report` wrapper, covering all four exits in one place.
    logger.info(
        "compose_report.peer_authored_finish turn=%s n_narratives=%d kinds=%s",
        turn_id,
        len(peer_narr),
        written,
    )
    return written


def _unauthored_substantial_remainder(
    peer_payloads: list[dict[str, Any]] | None,
    authored_owners: set[str],
) -> dict[str, Any] | None:
    """review #4 混合行文轮：有 peer 亲笔成文时，**没动笔**的 peer 交的实质
    payload 会整个掉地上（finisher 只收尾不成文）。取余量 = 「带 subagent_id、
    不在亲笔 owner 集、且有分析结论（`_payload_has_report_content` 同一实质
    判据）」的 entries，用 `_aggregate_subagent_payloads` 同一全保真合并聚成
    单代笔的输入。

    不带 subagent_id 的 entry（plan 路 step 形状只有 profile/payload/step_id）
    按「已被亲笔覆盖」保守排除——宁可少写一段，不冒双份正文的险（M-D 铁断言）。
    短答 payload 由聚合器内部的 `is_short_answer_payload` 排掉（chat 的归 chat）。
    """
    if not peer_payloads or not authored_owners:
        return None
    # Local import: lead_orchestrator imports this module only inside functions, with no directional cycle.
    # Reuse the same substantive-content criterion and merge policy without defining another.
    from bowhead_core.agents.lead_orchestrator import (
        _aggregate_subagent_payloads,
        _payload_has_report_content,
    )

    rem = [
        e
        for e in peer_payloads
        if isinstance(e, dict)
        and str(e.get("subagent_id") or "").strip()
        and str(e.get("subagent_id")) not in authored_owners
        and _payload_has_report_content(e.get("payload"))
    ]
    if not rem:
        return None
    return _aggregate_subagent_payloads(None, bag=rem)


def _section_alias_key(section: Any) -> str:
    """B1-7 提示行里的「所在位置」——把内部 kind 换成用户看得懂的别名键。"""
    kind = str(getattr(section, "kind", "") or "")
    payload = getattr(section, "payload", None)
    style = (payload or {}).get("style") if isinstance(payload, dict) else None
    if kind == "kpi_row":
        return "section_alias_kpi_row"
    if kind == "recommendations" or style == "recommendations":
        return "section_alias_recommendations"
    if kind == "narrative":
        return "section_alias_narrative"
    return "section_alias_other"


async def _revise_finish_if_applicable(
    *,
    canvas_store: Any,
    request: Any,
    bus: Any,
    payload: dict[str, Any],
    tool_invocations: list[Any] | None,
    artifact_store: Any = None,
) -> list[str] | None:
    """canvas-update-turn 批 B0 修订收尾器（最小版）。

    命中条件：本轮 patch 过上一轮的 narrative（`section_ids_touched_by_turn` ∩ 上一轮 narrative）
    **且**本轮没有 append h1（append 了 h1 = 新立文档，走原路）。
    命中 → 不发 header / kpi_row / recommendations / caveats（h1/KPI 带仍是旧口径，B1 做 patch 版），
    只补 ① 来源段（正文 bake 铸的 `[N]` 要有落点）② 溯源提示段；把 revised ids 挂到 bus
    （`_revised_section_ids`：终态判定与内容状态要把它们算进「本轮产出」）。返回**真实 kind**
    （`narrative`），不造下游不认识的串。未命中 → None。
    """
    from bowhead_core.canvas_revise import (
        note_revised_section_ids,
        revised_prior_narratives,
        turn_opened_new_document,
    )

    turn_id = getattr(request, "turn_id", None)
    # Review fix (plan perspective): plan execution bypasses revision finalization; handwritten finalization supplies its whole cover (h1/KPI/recommendation pool).
    # When rerunning steps updates prior chapters in place without a current h1 append, this match would swallow the cover.
    # Same criterion as `_alpha_fold_prior_blocks`' plan guard.
    if getattr(bus, "_d6_is_plan_execution", False):
        logger.info("compose_report.revise_skip turn=%s reason=plan_execution", turn_id)
        return None
    try:
        canvas = await canvas_store.get_canvas_by_session(request.session_id)
        if canvas is None or not turn_id:
            return None
        touched = await canvas_store.section_ids_touched_by_turn(turn_id)
        if not touched:
            return None
        all_secs = await canvas_store.list_sections(canvas.id)
        revised = revised_prior_narratives(all_secs, touched_ids=touched, this_turn_id=turn_id)
        if not revised:
            return None
        appended = await canvas_store.list_sections_appended_by_turn(turn_id)
        if turn_opened_new_document(appended):
            return None
    except Exception:
        logger.exception("compose_report.revise_detect_failed turn=%s", turn_id)
        return None  # Probe failure -> original path (fail open).
    if getattr(bus, "_md_finisher_done", False):
        return []
    with contextlib.suppress(Exception):
        bus._md_finisher_done = True  # Share duplicate-emission prevention with handwritten finalization.
    note_revised_section_ids(bus, [s.id for s in revised])
    written: list[str] = ["narrative"] * len(revised)
    try:
        reg = await get_turn_sources_registry(bus, canvas_store=canvas_store, canvas_id=canvas.id, turn_id=turn_id)
        reg.ingest_payload_sources((payload or {}).get("sources"), tool_invocations)
        reg.ingest_tool_invocations(tool_invocations)
        if not reg.is_empty():
            from bowhead_core.report_chrome_text import chrome_lang as _chrome_lang_r
            from bowhead_core.sources_resolver import enrich_source_labels

            _entries = await enrich_source_labels(reg.sources, artifact_store, user_id=getattr(request, "user_id", None))
            _sid = await upsert_turn_sources_section(
                canvas_store=canvas_store,
                bus=bus,
                canvas_id=canvas.id,
                turn_id=turn_id,
                entries=_entries,
                lang=_chrome_lang_r(getattr(request, "language_context", None)),
            )
            if _sid:
                written.append("sources")
        from bowhead_core.analysis_payload import provenance_caveats
        from bowhead_core.report_chrome_text import chrome_lang as _pv_lang_r
        from bowhead_core.report_chrome_text import chrome_text as _pv_text_r

        _lang_r = _pv_lang_r(getattr(request, "language_context", None))
        # Batch B1-1: patch chrome in place from supplied payload keys (meta changes only updated time); fold plan-chapter kpi_row with revisions.
        from bowhead_core.canvas_revise import document_block_for as _block_for
        from bowhead_core.canvas_revise import fold_revised_chapter_kpis as _fold_ch_kpis
        from bowhead_core.canvas_revise import patch_document_chrome as _patch_chrome
        from bowhead_core.canvas_revise import stale_values_after_revision as _stale_values

        _block = _block_for(await canvas_store.list_sections(canvas.id), revised[0].id)
        _incomplete_lines: list[str] = []
        _prev_by_id: dict[str, dict[str, Any]] = {}
        with contextlib.suppress(Exception):
            for _sid, _prev in await canvas_store.list_revisions_by_turn(turn_id):
                _prev_by_id.setdefault(_sid, _prev)  # First entry = before this turn's edit.
        if _block is not None:
            # 116 dogfood R12: partial plan-chapter edits have chapter-specific title/KPI; leave the document cover and that chapter's KPI band alone.
            from bowhead_core.canvas_revise import chapter_scoped_revision as _chapter_scope
            from bowhead_core.canvas_revise import patch_chapter_kpi_row as _patch_ch_kpi

            _ch_scope = _chapter_scope(_block, [s.id for s in revised], payload=payload or {}, this_turn_id=turn_id)
            # Batch B2-5 review P1-1: Lead explicitly naming revision scope outside plan documents (e.g. only store ranking) also means a partial edit.
            # `_chapter_scope` recognizes only plan chapters, missing this; otherwise cover title/KPI are overwritten from one section and contradict out-of-scope content.
            from bowhead_core.canvas_revise import revise_scope_bounded as _scope_bounded

            _bounded = _scope_bounded(bus)
            # Chapter-scoped edits leave **all** document chrome unchanged: title / cover KPI / recommendations / caveats / eyebrow summarize the whole document.
            # Stripping only kpis/title lets author caveats for chapter two replace whole-document caveats (review measured it).
            _chrome_payload = (
                {k: v for k, v in (payload or {}).items() if k not in ("kpis", "title", "recommendations", "caveats", "eyebrow")}
                if _ch_scope or _bounded
                else (payload or {})
            )
            if _ch_scope:
                logger.info("compose_report.revise_chapter_scope turn=%s chapters=%s", turn_id, [c[0] for c in _ch_scope])
            _chrome = await _patch_chrome(
                canvas_store, bus, canvas_id=canvas.id, turn_id=turn_id, block=_block, payload=_chrome_payload, lang=_lang_r
            )
            # `written` lists **section kinds**, used downstream to determine report production. Chrome's returned
            # `caveats` is a narrative style, not a kind; map it back rather than adding a new term here.
            written += [("narrative" if k == "caveats" else k) for k in (_chrome.get("patched") or [])]
            # Batch B1-6: revisions also emit `canvas.opened` (existing event; frontend keeps sections on the same canvas) with `revised=true`;
            # IM cards use it for an updated-title first line without h1; frontend reads only canvas_id/session_id/title.
            with contextlib.suppress(Exception):
                # ⚠ `patched` contains **section kinds** (h1/kpi_row/…), not payload keys. If title changed this turn, use the new payload title;
                # otherwise fall back to block h1 text (`_block` is a prepatch snapshot; reading it directly emits old titles, found in all three reviews).
                _title = (payload or {}).get("title") if "h1" in (_chrome.get("patched") or []) else None
                _title = str(_title or (getattr(_block.h1, "payload", None) or {}).get("text") or canvas.title or "").strip() or None
                await bus.emit(
                    _event(
                        turn_id,
                        "canvas.opened",
                        {"canvas_id": canvas.id, "session_id": canvas.session_id, "title": _title, "revised": True},
                    )
                )
            if (payload or {}).get("kpis"):
                if _ch_scope:
                    # One revised chapter -> write KPI to its own band; multiple but not all chapters -> a KPI set has ambiguous ownership, so do not guess.
                    if len(_ch_scope) > 1:
                        logger.info(
                            "compose_report.revise_chapter_kpi_skip turn=%s chapters=%s reason=multi_chapter_scope",
                            turn_id,
                            [c[0] for c in _ch_scope],
                        )
                    elif await _patch_ch_kpi(
                        canvas_store, bus, canvas_id=canvas.id, turn_id=turn_id, block=_block, chapter=_ch_scope[0], payload=payload or {}
                    ):
                        written.append("kpi_row")
                elif not _bounded:  # Named scope without a chapter: partial revision; preserve other chapter KPIs.
                    await _fold_ch_kpis(canvas_store, bus, canvas_id=canvas.id, turn_id=turn_id, block=_block, revised_ids=[s.id for s in revised])
            # Batch B1-2: fold old charts no longer referenced after replacement, plus associated code blocks.
            from bowhead_core.canvas_revise import fold_dropped_figures as _fold_figs

            _folded_figs = await _fold_figs(canvas_store, bus, canvas_id=canvas.id, turn_id=turn_id, revised=revised, prev_by_id=_prev_by_id)
            # Batch B1-7 revision integrity: old changed values remain in other block sections' **rendered fields** -> warn in provenance only, never auto-edit.
            with contextlib.suppress(Exception):
                _fresh = await canvas_store.list_sections(canvas.id)
                _block2 = _block_for(_fresh, revised[0].id)
                _revised_fresh = [s for s in _fresh if s.id in {r.id for r in revised}]
                if _block2 is not None:
                    _stale = _stale_values(_block2, _revised_fresh, _prev_by_id, chapter_scope=_ch_scope)
                    if _stale:
                        logger.info(
                            "compose_report.revise_incomplete turn=%s sections=%s stale_values=%s",
                            turn_id,
                            [s.id for s, _ in _stale],
                            sorted({v for _, vs in _stale for v in vs})[:12],
                        )
                        # This is **user-visible text**: localize separators and use readable section aliases, never internal kinds.
                        _sep = "、" if str(_lang_r).lower().startswith("zh") else ", "
                        _tpl = _pv_text_r("revise_incomplete_line", _lang_r, values="{values}", kinds="{kinds}")
                        for _sec, _vals in _stale:
                            _incomplete_lines.append(_tpl.format(values=_sep.join(_vals[:6]), kinds=_pv_text_r(_section_alias_key(_sec), _lang_r)))
        _prov = list(provenance_caveats(payload or {})) + _incomplete_lines
        if _prov:
            # Batch B1-5: merge by line into existing document-block provenance, or create with _meta.provenance if absent.
            from bowhead_core.canvas_revise import upsert_provenance_section as _upsert_prov

            _heading = _pv_text_r("provenance_heading", _lang_r)
            _res = await _upsert_prov(
                canvas_store, bus, canvas_id=canvas.id, turn_id=turn_id, anchor_id=revised[0].id, heading=_heading, lines=list(_prov)
            )
            if _res:
                written.append("provenance_caveats")
        # Batch B1-4 mixed turns patch prior sections and append chapters: insert current prose before document tail sections.
        from bowhead_core.canvas_revise import place_appended_before_tail as _place_r

        _moved = await _place_r(canvas_store, bus, canvas_id=canvas.id, turn_id=turn_id)
        if _moved:
            logger.info("compose_report.append_place turn=%s moved=%d via=revise_finish", turn_id, _moved)
    except Exception:
        logger.exception("compose_report.revise_finish_failed turn=%s", turn_id)
    logger.info(
        "compose_report.revise_finish turn=%s revised=%d kinds=%s",
        turn_id,
        len(revised),
        written,
    )
    return written


async def _compose_report_impl(
    *,
    canvas_store: Any,
    request: Any,
    bus: Any,
    payload: dict[str, Any],
    peer_payloads: list[dict[str, Any]] | None = None,
    tool_invocations: list[Any] | None = None,
    router: Router,
    agent_type: str = "narrative",  # Retained call compatibility; finalization does not invoke a model.
    artifact_store: Any = None,
    usage_out: list[tuple[int, int, int]] | None = None,
    intent: Any = None,  # Card default ghostwriting forwards to compose_canvas_from_payload (meta pill).
) -> list[str]:
    """Finalize Agent-authored sections and compose remaining payload deterministically."""
    reject_unclassified_direct_mutation("report.compose")
    raise_if_terminal_bus_failed(bus)
    # canvas-update-turn batch B0: revisions patch a prior-turn narrative without creating a new document
    # (no h1 append). They do not count as this turn's appended handwritten content, which finalization below detects by append;
    # previously they fell into default ghostwriter cards and added another h1/KPI/insight set. Use revision finalization without chrome,
    # adding only sources/provenance (B1 then adds patched chrome).
    from bowhead_core.canvas_revise import section_revise_enabled as _revise_on

    if _revise_on():
        _revised = await _revise_finish_if_applicable(
            canvas_store=canvas_store,
            request=request,
            bus=bus,
            payload=payload,
            tool_invocations=tool_invocations,
            artifact_store=artifact_store,
        )
        if _revised is not None:
            return _revised
    authored_owners: set[str] = set()
    finished = await _maybe_finish_peer_authored_report(
        canvas_store=canvas_store,
        request=request,
        bus=bus,
        payload=payload,
        tool_invocations=tool_invocations,
        artifact_store=artifact_store,
        authored_owners_out=authored_owners,
    )
    if finished is not None:
        # Review #4 mixed-writing turns: besides handwritten peers, peers submitted substantive payloads without writing;
        # write only this remainder. Finalization already emitted bands/Sources from the **aggregated** payload
        # (including remainder peers' KPIs). Disable emit_band in ghostwriting to prevent duplication; Sources uses
        # upsert to the same turn/section, with numbering unified by the shared bus registry.
        # In card mode ψ-E eager already rendered nonwriting peers' payloads as cards on return
        # (_psi_compose_done); remainder content is on the canvas and must never be written again.
        remainder = None
        if not getattr(bus, "_psi_compose_done", False):
            remainder = _unauthored_substantial_remainder(peer_payloads, authored_owners)
        if remainder is not None:
            # Card default ghostwriting without eager (PSI flag disabled/no bus): render remainder deterministically as cards.
            logger.info(
                "compose_report.mixed_authorship_remainder turn=%s authored=%d fields=%s mode=cards",
                getattr(request, "turn_id", None),
                len(authored_owners),
                sorted(remainder.keys()),
            )
            from bowhead_core.canvas_composer import compose_canvas_from_payload
            from bowhead_core.canvas_revise import no_document_chrome as _no_chrome_r
            from bowhead_core.canvas_revise import strip_document_chrome_payload as _strip_chrome_r

            _rem_no_chrome = _no_chrome_r(bus)  # B0 review fix: revise/append remainder cards create no h1.
            finished += await compose_canvas_from_payload(
                canvas_store=canvas_store,
                request=request,
                bus=bus,
                payload=_strip_chrome_r(remainder, bus) if _rem_no_chrome else remainder,
                intent=None,
                tool_invocations=tool_invocations,
                artifact_store=artifact_store,
                chapter_mode=_rem_no_chrome,
            )
        else:
            logger.info(
                "compose_report.peer_authored_no_remainder turn=%s authored=%d psi_done=%s",
                getattr(request, "turn_id", None),
                len(authored_owners),
                bool(getattr(bus, "_psi_compose_done", False)),
            )
        return finished
    # Default ghostwriting uses deterministic cards (default, user decision 2026-08-13). Render here, not through
    # caller fallback for empty kinds: [] also prevents duplicate emission, so that detour recomposes already-composed turns.
    logger.info(
        "compose_report.scribe_cards turn=%s mode=deterministic",
        getattr(request, "turn_id", None),
    )
    from bowhead_core.canvas_composer import compose_canvas_from_payload
    from bowhead_core.canvas_revise import no_document_chrome as _no_chrome_s
    from bowhead_core.canvas_revise import strip_document_chrome_payload as _strip_chrome_s

    # canvas-update-turn batch B0 (review fix): revise/append default ghostwriting must not create a new document
    # (remove title + chapter_mode), matching eager cards and handwritten finalization.
    _scribe_no_chrome = _no_chrome_s(bus)
    if _scribe_no_chrome:
        logger.info("compose_report.scribe_cards_mode_no_chrome turn=%s", getattr(request, "turn_id", None))
    _kinds = await compose_canvas_from_payload(
        canvas_store=canvas_store,
        request=request,
        bus=bus,
        payload=_strip_chrome_s(payload, bus) if _scribe_no_chrome else payload,
        intent=intent,
        tool_invocations=tool_invocations,
        artifact_store=artifact_store,
        chapter_mode=_scribe_no_chrome,
    )
    return _kinds


async def compose_report(
    *,
    canvas_store: Any,
    request: Any,
    bus: Any,
    payload: dict[str, Any],
    peer_payloads: list[dict[str, Any]] | None = None,
    tool_invocations: list[Any] | None = None,
    router: Router,
    agent_type: str = "narrative",
    artifact_store: Any = None,
    usage_out: list[tuple[int, int, int]] | None = None,
    intent: Any = None,
) -> list[str]:
    """Finalize layout and record substantial content after every composition path.

    The shared wrapper covers peer-authored, revision and deterministic content.
    Assessment is best-effort: unknown status does not mutate the record, and a
    failed annotation cannot discard a report that has already been written.
    Router, agent_type and usage_out remain compatible internal call arguments;
    this finalizer no longer calls a prose-scribe model.
    """
    kinds = await _compose_report_impl(
        canvas_store=canvas_store,
        request=request,
        bus=bus,
        payload=payload,
        peer_payloads=peer_payloads,
        tool_invocations=tool_invocations,
        router=router,
        agent_type=agent_type,
        artifact_store=artifact_store,
        usage_out=usage_out,
        intent=intent,
    )
    # Apply final layout actions in one wrapper so every composition path gets them.
    # Order chrome promotion -> canonical sorting: promotion moves current h1 first; segmentation uses
    # that first h1 position, so reversing them creates boundaries elsewhere.
    await _promote_document_chrome_best_effort(canvas_store, bus, request)
    with contextlib.suppress(Exception):
        _cv_sort = await canvas_store.get_canvas_by_session(getattr(request, "session_id", "") or "")
        if _cv_sort is not None:
            await _apply_canonical_order(canvas_store, bus, _cv_sort.id, getattr(request, "turn_id", None))
    try:
        from bowhead_core.canvas_content_status import assess_canvas_content, tool_content_signal

        _turn_id = getattr(request, "turn_id", None)
        _canvas = await canvas_store.get_canvas_by_session(getattr(request, "session_id", "") or "")
        # ★★`kinds` records **what the composer rendered**, not everything added to the canvas this turn. They differ greatly on
        # **handwritten paths** (main path since sprint batch 3): peers directly write charts/tables via canvas tools,
        # while finalization's `written` vocabulary is only {eyebrow,h1,meta,kpi_row,narrative,sources,
        # caveats,recommendations,provenance_caveats}, with **no `_FIGURE_KINDS`**.
        # Looking only at `kinds` makes `visual_output` **structurally unreachable** for handwritten turns.
        # 116 dogfood `t_c5c57f197cd7`: canvas contains meta/h1/kpi_row/narrative/
        # data_table×2/sources/code_block×2, but `compose_report.peer_authored_finish`
        # logs `kinds=['narrative','meta','sources']`, leaving the criterion undecided and recording nothing.
        # Include **visible** current-turn canvas sections, matching report-library criteria that exclude hidden/superseded.
        _turn_kinds = set(kinds)
        # canvas-update-turn batch B0: prior-turn sections patched in revisions also count as current output;
        # otherwise a thin revision with zero tool rows marks the whole canvas empty (review P2).
        from bowhead_core.canvas_revise import revised_section_ids as _revised_ids

        _revised_set = set(_revised_ids(bus))
        if _canvas is not None:
            for _s in await canvas_store.list_sections(_canvas.id):
                if _s.id in _revised_set:
                    _turn_kinds.add(_s.kind)
                    continue
                if getattr(_s, "turn_id", None) != _turn_id:
                    continue
                if getattr(_s, "hidden_for_chat_mode", False) or getattr(_s, "superseded_by_turn", None):
                    continue
                _turn_kinds.add(_s.kind)
        # ★ The signal is whether retrieval/computation tools **returned rows**, not antifabrication's observed-number set
        # (review §5.10 R-1): that set counts any result number (`row_count: 0`,
        # `elapsed_ms`, `status: 504`) and was **always true** across 549 database turns on 116, never allowing empty.
        _signal = tool_content_signal(tool_invocations)
        _status, _reason = assess_canvas_content(
            payload=payload,
            emitted_kinds=_turn_kinds,
            tool_signal=_signal,
        )
        if _status and _canvas is not None:
            await canvas_store.set_content_status(_canvas.id, _status)
            logger.info(
                "compose_report.content_status turn=%s status=%s reason=%s signal=%s composed=%s on_canvas=%s",
                _turn_id,
                _status,
                _reason,
                _signal,
                kinds[:8],
                sorted(_turn_kinds)[:10],
            )
    except Exception:
        # Marking is supplementary: classification/persistence failures must not affect an already written report.
        logger.exception("compose_report.content_status_failed turn=%s", getattr(request, "turn_id", None))
    return kinds

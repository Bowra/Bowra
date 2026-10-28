"""正文图锚 `[figure:<id>]` 的**位置归一**——写入侧单一真源。

## 为什么需要它

画布的 PM 读路（`lib/canvas-pm-adapter/markdown-pm.ts` 的 `liftFigureAnchors`）只提升
**段落首个文本节点开头**的锚。其余位置——句中、行尾、与上文同段的独占行、叠放的第二个锚、
列表/引用/表格/标题里的锚——一律原样显示成 `[figure:sec_xxx]` 字面量，而被引用的图仍孤零零
留在它自己的 seq 位置。116 实测：2244 个锚里 49 个是这种漏出形态，占比最大的是「独占一行但
与上文同段」（23 个）。

老修复 `report_writer_loop._strip_invalid_figure_anchors`（08-28 review F4）只管「独占行」与
「行尾」两种，且**它把行尾锚改写成的 `正文\\n[figure:x]` 在画布上照样漏**——它是按 chat 渲染器
（`lib/md.ts` 逐行切段）的语义写的，而画布是另一套解析器。加上 `canvas_patch_section` 那条写路
完全不过 bake，于是续轮修订一开，老形态就从新入口回来了。

所以判据下沉到**存储写入口**：`CanvasStore.append_section` 与 `CanvasStore.patch_section`
两处（原第三处 `AtomicCanvasOps._apply_in_db` 已随章节编辑工具删除），任何持笔者一视同仁（拆段/合段这类没带新正文的
结构操作不挂，见不变量 5）。设计见
`docs/sprints/canvas-update-turn-2026-09-03/tech-design-figure-anchor.md`。

## 不变量

1. **没有错位锚的 markdown 字节级不变**——这道闸的职责是挪锚，不是顺手重排正文空白。
   （例外：同一个 id 写了两次且两次都已经是段首锚时，本闸不管——那是既有渲染行为，
   收敛它会改动「本来没有错位锚」的正文字节，代价大于收益。去重只作用于**被挪动的**锚。）
2. **输出是不动点**：归一后的文本再跑一次一定无改动（所有锚都成了段首锚）。
3. **不碰代码与链接**：围栏、缩进代码块、行内代码、链接/图片的目标里出现的方括号都不是引用。
4. **不拆块**：列表/引用/表格/标题里的锚挪到**整块之后**，不在块中间切开。
   ⚠ 已知限制：**松散列表**（条目之间有空行）在行扫描口径下每个条目各算一块，
   图会落在两个条目之间，把一个列表断成两个。要修得上真正的 CommonMark 块解析，
   代价大于收益——空行本来就是块分隔，这里按已知限制记着。
5. **没带正文的写入一个字节都不碰**：存储层只在这次写入真的带了 `markdown` 时才过闸。
   只改 `_meta`/`source_refs`、以及拆段/合段这类结构操作一律跳过——不删内容，也**不改写句子**
   （句中锚一挪就会把 token 换成「图标题」，用户按一下「拆段」发现句子变了，比漏个锚更意外）。
   位置自愈交给真正带正文的写入与存量作业。纯函数侧的 `strip_invalid=False` 供存量作业用：
   只挪位置、不摘无效锚。
"""

from __future__ import annotations

import re
from typing import Any

#: Section kinds addressable by [figure:id] (visual artifacts). Single source of truth in core:
#: agents/report_tools._FIGURE_KINDS and canvas_revise._FIGURE_KINDS_LOCAL both alias this set.
FIGURE_KINDS = frozenset(
    {
        "chart",
        "image",
        "design",
        "html_artifact",
        "datarc_iframe",
        "external_iframe",
        "data_table",
    }
)

#: Kinds whose body is a payload markdown string (frontend textBasedToPMNode → markdownToPMNodes).
MARKDOWN_PAYLOAD_KINDS = frozenset({"narrative", "deck", "callout"})

# Same character class as export_ir._FIGURE_ANCHOR_RE / chat_figure_anchors._FIGURE_RE /
# md.ts::_FIGURE_INLINE_RE.
_FIGURE_RE = re.compile(r"\[figure:([A-Za-z0-9_]+)\]")
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")
#: Indented code (four spaces or a leading tab) is code in CommonMark, not body prose. Prefer missed moves to corrupted code.
_INDENTED_CODE_RE = re.compile(r"^(?: {4}|\t)")
#: Blocks starting with these markers are not ordinary paragraphs: frontend never lifts anchors inside lists/quotes/headings/tables,
#: and such blocks must not be split midway.
_NON_PARAGRAPH_RE = re.compile(r"^[ \t]*(?:[-*+][ \t]|\d+[.)][ \t]|>|#|\|)")
_ATX_HEADING_RE = re.compile(r"^[ \t]*#")
#: Inline code and link/image destinations: their brackets are not references.
_INLINE_CODE_RE = re.compile(r"`+[^`]*`+")
_LINK_DEST_RE = re.compile(r"\]\([^)]*\)")
#: Anchor used as link text: [figure:x](url). Rewriting it breaks the link into scattered text plus bare (url).
_LINK_LABEL_RE = re.compile(r"\[figure:[A-Za-z0-9_]+\]\(")
#: HTML blocks terminate only at blank lines in CommonMark; inserting one midway renders closing tags literally.
_HTML_BLOCK_RE = re.compile(r"^[ \t]*(?:</?[A-Za-z]|<!--)")
_CJK_RE = re.compile(r"[　-〿一-鿿＀-￯]")
#: Lines reduced to block markers after token removal (empty list items/quotes/headings): discard the entire line.
_BARE_MARKER_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)]|>+|#{1,6})[ \t]*$")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]")
_OPEN_BRACKETS = ("（", "(")
_CLOSE_BRACKETS = ("）", ")")

_CAPTION_RE_CACHED: re.Pattern[str] | None = None


def _caption_re() -> re.Pattern[str]:
    """图注前缀正则——**复用 `export_ir.FIGURE_CAPTION_PREFIX_RE`，绝不新写第三份**。

    它与前端 `extensions/figure-caption.ts` 的 `CAPTION_RE` 是跨语言双份常量，
    `test_figure_caption_parity.py` 从 TS 源码里抠正则字面逐字对拍；自己再写一份
    就是绕开那道闸（两侧行为真的分叉过）。

    lazy import：`export_ir` 拖着整条导出依赖链，而本模块要被 `canvas_store` 顶层
    引用——存储层不该因为一个正则把导出链拉进 import 图。
    """
    global _CAPTION_RE_CACHED
    if _CAPTION_RE_CACHED is None:
        from bowhead_core.export_ir import FIGURE_CAPTION_PREFIX_RE

        _CAPTION_RE_CACHED = FIGURE_CAPTION_PREFIX_RE
    return _CAPTION_RE_CACHED


def figure_title(payload: Any, kind: str) -> str:
    """图的标题，**取不到就回 kind**——给证据清单这类内部展示用（原 `report_tools._fig_title`）。"""
    spec = payload.get("spec") if isinstance(payload, dict) else None
    if isinstance(spec, dict) and spec.get("title"):
        return str(spec["title"])
    if isinstance(payload, dict):
        return str(payload.get("title") or payload.get("caption") or kind)
    return kind


def figure_prose_title(payload: Any, kind: str) -> str:
    """要写进**用户正文**的标题：取不到就返回空串，绝不回退成 kind 名。

    句中锚会被这个值替换掉。`figure_title` 的 kind 兜底本来只喂内部证据清单，拿来替换
    正文就会在句子里渲出「data_table」「html_artifact」这种机器词（无标题的表很常见）。
    取不到标题时宁可只摘 token。
    """
    spec = payload.get("spec") if isinstance(payload, dict) else None
    if isinstance(spec, dict) and spec.get("title"):
        return str(spec["title"])
    if isinstance(payload, dict):
        return str(payload.get("title") or payload.get("caption") or "")
    return ""


def _safe_title(title: str) -> str:
    """标题要写进正文，先把里面的锚形 token 摘掉。

    标题是模型写的，真出现 `[figure:x]` 时替换进去会让输出不再是不动点——
    下一次写入会把它当成一个新锚再挪一次，用户看到的句子又变了（review 二轮 #6）。
    """
    return re.sub(r"[ \t]{2,}", " ", _FIGURE_RE.sub("", title)).strip()


def _is_caption_line(line: str) -> bool:
    """是不是紧跟图的「图N：」图注行。

    ⚠ 自己还带着锚的行**不算**图注：图注是整行原样搬到块尾的，不会再过一遍归一，
    于是那个锚永远留在里面——每次写入都再动一次，输出不成不动点（review 四轮 #5）。
    """
    if "[figure:" in line:
        return False
    return bool(_caption_re().match(line.strip()))


def _quote(title: str) -> str:
    """引号只看**标题本身**的语言：中文标题用「」，英文标题用 “”。"""
    return f"「{title}」" if _CJK_RE.search(title) else f"“{title}”"


def _protected_spans(line: str) -> list[tuple[int, int]]:
    """行内代码与链接目标的区间——里面的 `[figure:x]` 是代码/URL 的一部分，不是引用。"""
    spans = [(m.start(), m.end()) for m in _INLINE_CODE_RE.finditer(line)]
    spans += [(m.start(), m.end()) for m in _LINK_DEST_RE.finditer(line)]
    spans += [(m.start(), m.end() - 1) for m in _LINK_LABEL_RE.finditer(line)]
    return spans


def _in_protected(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def _substitute(line: str, start: int, end: int, title: str) -> str:
    """把行里 [start,end) 这段 token 换成标题（title 为空则摘掉），并收拾两侧空白与空括号。"""
    left, right = line[:start], line[end:]
    lstripped, rstripped = left.rstrip(" \t"), right.lstrip(" \t")
    if not title:
        # ⚠ Clean only the empty bracket pair **immediately enclosing this token**: the old whole-line regex
        # also removed existing `（）` in body text (demonstrated in review #4).
        if lstripped.endswith(_OPEN_BRACKETS) and rstripped.startswith(_CLOSE_BRACKETS):
            return lstripped[:-1] + rstripped[1:]
        # ⚠ Keep a space when both sides are ASCII word characters: concatenating `see [figure:x] for` directly yields seefor.
        # Title replacement below already did this; token stripping omitted it. Existing cases all used Chinese prose,
        # which needs no space, so the omission went unnoticed (review round 5 #1).
        joiner = " " if (_ASCII_WORD_RE.search(lstripped[-1:]) and _ASCII_WORD_RE.search(rstripped[:1])) else ""
        return lstripped + joiner + rstripped
    repl = _quote(title)
    # Add spacing when surrounding characters were ASCII word characters (`see [figure:x] for` → `see “T” for`).
    if _ASCII_WORD_RE.search(lstripped[-1:]):
        repl = " " + repl
    if _ASCII_WORD_RE.search(rstripped[:1]):
        repl = repl + " "
    return lstripped + repl + rstripped


class _Anchor:
    """一次锚出现：行号、span、位置分类、所在块是否普通段落。"""

    __slots__ = ("end", "fid", "in_paragraph", "kind", "line_no", "start")

    def __init__(self, line_no: int, start: int, end: int, fid: str, kind: str, in_paragraph: bool) -> None:
        self.line_no = line_no
        self.start = start
        self.end = end
        self.fid = fid
        self.kind = kind  # lead | own_line | trailing | mid
        self.in_paragraph = in_paragraph


def _scan(lines: list[str]) -> tuple[list[_Anchor], set[str], set[int], set[int]]:
    """第一趟：只分类，不改字。

    返回 (锚列表, 已是块级锚的 id, 每个块最后一行的行号, 不许改写的行号)。
    """
    anchors: list[_Anchor] = []
    lead_ids: set[str] = set()
    block_last: set[int] = set()
    frozen: set[int] = set()  # Fences/indented code: preserve every byte
    in_fence = False
    block_first = True
    block_is_para = True
    prev_content_line = -1
    prev_blank = True
    in_html_block = False
    in_indented_code = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            if prev_content_line >= 0:
                block_last.add(prev_content_line)
            prev_content_line = -1
            in_fence = not in_fence
            block_first = True
            frozen.add(i)
            prev_blank = False
            continue
        if in_fence:
            frozen.add(i)
            prev_blank = False
            continue
        if not line.strip():
            if prev_content_line >= 0:
                block_last.add(prev_content_line)
            prev_content_line = -1
            block_first = True
            prev_blank = True
            in_html_block = False  # Blank line terminates HTML blocks (CommonMark)
            in_indented_code = False
            continue
        # Indented code exists only between paragraphs (list continuations also have four spaces but block classification covers them):
        # conservatively treat indentation after a blank line as code; do not rewrite it.
        if _HTML_BLOCK_RE.match(line):
            # CommonMark type-6 HTML blocks **can interrupt paragraphs**, so do not require a preceding blank line.
            in_html_block = True
        if _INDENTED_CODE_RE.match(line) and (prev_blank or in_indented_code):
            in_indented_code = True
        if in_indented_code and _INDENTED_CODE_RE.match(line):
            frozen.add(i)
            prev_content_line = i
            block_first = False
            prev_blank = False
            continue
        in_indented_code = False
        if in_html_block:
            frozen.add(i)
            prev_content_line = i
            block_first = False
            prev_blank = False
            continue
        prev_blank = False
        is_first = block_first
        if is_first:
            block_is_para = not _NON_PARAGRAPH_RE.match(line)
        block_first = False
        is_heading = bool(_ATX_HEADING_RE.match(line))
        spans = _protected_spans(line)
        for m in _FIGURE_RE.finditer(line):
            if _in_protected(spans, m.start(), m.end()):
                continue  # Brackets inside inline code/link destinations are not references
            before, after = line[: m.start()], line[m.end() :]
            if is_first and block_is_para and not before.strip():
                kind = "lead"
                lead_ids.add(m.group(1))
            elif not before.strip() and not after.strip():
                kind = "own_line"
            elif not after.strip():
                kind = "trailing"
            else:
                kind = "mid"
            anchors.append(_Anchor(i, m.start(), m.end(), m.group(1), kind, block_is_para))
        if is_heading:
            # ATX headings form their own block; the immediately following body starts another.
            block_last.add(i)
            block_first = True
            prev_content_line = -1
        else:
            prev_content_line = i
    if prev_content_line >= 0:
        block_last.add(prev_content_line)
    return anchors, lead_ids, block_last, frozen


def normalize_figure_anchors(md: str, figures: dict[str, str], *, strip_invalid: bool = True) -> tuple[str, dict[str, int]]:
    """把错位的 `[figure:id]` 归一成段首锚；无效 id 摘掉（``strip_invalid=False`` 时留着）。

    ``figures``：本画布里可被引用的图 ``{section_id: 用于正文替换的标题}``；标题可为空串
    （见 :func:`figure_prose_title`），此时句中锚只摘 token 不替换。
    返回 ``(新 markdown, stats)``；``stats["changed"]`` 为 0 时返回的是**原字符串**。
    """
    stats = {"moved": 0, "stripped": 0, "titled": 0, "changed": 0}
    if not md or "[figure:" not in md:
        return md, stats

    crlf = "\r\n" in md and "\n" not in md.replace("\r\n", "")
    work = md.replace("\r\n", "\n") if crlf else md
    trailing_newline = work.endswith("\n")
    lines = work.split("\n")
    anchors, lead_ids, block_last, frozen = _scan(lines)
    needs_work = [a for a in anchors if a.kind != "lead" or (strip_invalid and a.fid not in figures)]
    if not needs_work:
        return md, stats  # Invariant 1: no misplaced anchors or invalid anchors to strip → no byte changes

    by_line: dict[int, list[_Anchor]] = {}
    for a in anchors:
        by_line.setdefault(a.line_no, []).append(a)

    placed: set[str] = {fid for fid in lead_ids if fid in figures}
    out: list[str] = []
    # Anchors to insert after this block: (id, accompanying caption line). **Flush all at block end**:
    # formerly trailing anchors split paragraphs in place, reversing their order relative to mid-sentence anchors in the same block (review #2).
    pending: list[tuple[str, str | None]] = []
    want_blank = False  # Require a blank line before the next actual content line

    def emit(line: str) -> None:
        nonlocal want_blank
        if want_blank and out and out[-1].strip():
            out.append("")
        want_blank = False
        out.append(line)

    def emit_original_blank(line: str) -> None:
        nonlocal want_blank
        want_blank = False
        if out and not out[-1].strip():
            # Collapse consecutive blank lines to one. Once this gate acts it rewrites the document, making this whitespace cleanup acceptable;
            # preserving them would leave doubled gaps where content (invalid anchors + orphan captions) was removed.
            return
        out.append(line)

    def flush_pending() -> None:
        nonlocal want_blank
        if not pending:
            # ⚠ Empty flush must not set want_blank: the fence branch flushes before emitting the line;
            # setting it at the closing fence would insert a blank line **inside the code block** (review round 2 #1).
            return
        for fid, caption in pending:
            if out and out[-1].strip():
                out.append("")
            out.append(f"[figure:{fid}]")
            if caption is not None:
                out.append(caption)  # Caption immediately follows anchor without a blank (RC-8 attachment rule)
        pending.clear()
        want_blank = True

    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            flush_pending()
            in_fence = not in_fence
            emit(line)
            i += 1
            continue
        if in_fence or i in frozen:
            out.append(line)  # Fenced/indented code: preserve even whitespace
            i += 1
            continue
        if not line.strip():
            flush_pending()
            emit_original_blank(line)
            i += 1
            continue

        here = by_line.get(i) or []
        if not here:
            emit(line)
            if i in block_last:
                flush_pending()
            i += 1
            continue

        caption_idx = i + 1 if (i + 1 < len(lines) and _is_caption_line(lines[i + 1])) else None

        # ── A line containing only one anchor ──
        only = here[0]
        if len(here) == 1 and only.kind == "own_line":
            valid = only.fid in figures
            dup = only.fid in placed
            consumed = caption_idx if caption_idx is not None else i
            if not valid and not strip_invalid:
                emit(line)  # Move only, do not delete: caller did not change body; preserve invalid anchors
                if caption_idx is not None:
                    out.append(lines[caption_idx])
                i = consumed + 1
                if consumed in block_last:
                    flush_pending()
                continue
            if not valid or dup:
                stats["stripped"] += 1
                stats["changed"] = 1
                i = consumed + 1  # Remove anchor and orphan caption together
                if consumed in block_last:
                    flush_pending()
                continue
            if only.in_paragraph:
                # Standalone anchor line inside a paragraph: split into its own paragraph in place.
                # Require a blank before the standalone anchor; otherwise CommonMark joins it to the prior paragraph and PM cannot lift it.
                want_blank = True
                emit(line.strip())
                placed.add(only.fid)
                stats["moved"] += 1
                stats["changed"] = 1
                if caption_idx is not None:
                    out.append(lines[caption_idx])
                want_blank = True
            else:
                # Standalone anchor inside lists/quotes/tables: do not split the block; move anchor **and caption** to block end.
                pending.append((only.fid, lines[caption_idx] if caption_idx is not None else None))
                placed.add(only.fid)
                stats["moved"] += 1
                stats["changed"] = 1
            i = consumed + 1
            if consumed in block_last:
                flush_pending()
            continue

        # ── Separate decisions from rewriting: **decide dispositions in document order first** (block anchor/title replacement/strip),
        #    then rewrite right to left to preserve earlier anchor span offsets. Combining these reverses deduplication:
        #    the first encountered from the right (actually the last in the body) survives as the first occurrence, while
        #    the leftmost becomes a duplicate, swapping inline figures when export orders by anchors (caught by test_canvas_export_projection).
        new_line = line
        line_pending: list[str] = []
        decisions: dict[int, tuple[str, str]] = {}  # anchor.start → (disposition, replacement title)
        for a in sorted(here, key=lambda x: x.start):
            if a.kind == "lead" and a.fid in figures:
                continue  # Preserve valid paragraph-leading anchors
            if a.fid not in figures:
                if strip_invalid:
                    decisions[a.start] = ("strip", "")
                continue  # strip_invalid=False: preserve every invalid-anchor byte
            title = _safe_title(figures.get(a.fid) or "") if a.kind == "mid" else ""
            if a.fid in placed:
                decisions[a.start] = ("dup", title)  # Figure is already a block; do not emit it twice
                continue
            placed.add(a.fid)
            line_pending.append(a.fid)
            decisions[a.start] = ("move", title)

        for a in sorted(here, key=lambda x: x.start, reverse=True):
            decided = decisions.get(a.start)
            if decided is None:
                continue
            action, title = decided
            new_line = _substitute(new_line, a.start, a.end, title)
            if title:
                stats["titled"] += 1
            if action == "move":
                stats["moved"] += 1
            else:
                stats["stripped"] += 1
            stats["changed"] = 1

        moved_any = bool(line_pending)
        # Attach this line's anchors at block end in document order (pending accumulates across lines in body order).
        # Caption follows the **last** anchor moved from this line: an emptied line has nothing else it can attach to.
        line_emptied = not new_line.strip() or bool(_BARE_MARKER_RE.match(new_line))
        # Caption belongs to **the figure**, not remaining prose: whenever an anchor moves from this line, its following `图N：`
        # moves too. Requiring an entirely emptied line left captions for trailing/mid-sentence anchors behind,
        # separating figures from captions and mixing captions into preceding body paragraphs (review round 3 #1).
        carry_caption = caption_idx is not None and moved_any
        for idx, fid in enumerate(line_pending):
            is_last = idx == len(line_pending) - 1
            pending.append((fid, lines[caption_idx] if (carry_caption and is_last) else None))

        consumed = i
        if not line_emptied:
            # Do not rstrip: two trailing spaces are a Markdown hard break; stripping joins two lines.
            emit(new_line)
        if carry_caption or (caption_idx is not None and not moved_any and line_emptied):
            # Captions either move with anchors to block end or are removed as orphans when all anchors are invalid and stripped;
            # neither case should emit them again here.
            consumed = caption_idx

        if consumed in block_last or (consumed != i and i in block_last):
            flush_pending()
        i = consumed + 1

    flush_pending()
    while out and not out[-1].strip():
        out.pop()
    result = "\n".join(out)
    if trailing_newline:
        result += "\n"
    if crlf:
        result = result.replace("\n", "\r\n")
    if not stats["changed"] or result == md:
        # No disposition applied (or byte-identical result) → always return the **original string** and clear statistics;
        # otherwise callers receive whitespace-reordered text and logs falsely claim N anchors moved.
        return md, {"moved": 0, "stripped": 0, "titled": 0, "changed": 0}
    return result, stats

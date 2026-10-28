"""亲笔正文按章切段（canvas-elements 第二步，tech-design-chapter-split.md §2.1）。

peer 用 `write_report` 交的是整篇 markdown；116 语料 74.5% 的亲笔段自己就用 `## ` 分章
（中位 3 章）。修订单位是段，整篇一段 ⇒「改摘要」= 整段重写、三章丢失（dogfood §4.1）。
本模块把整篇按**行首 `## `** 切成「导语 + 每章一段」——纯函数、无 IO、**字节守恒**
（各段 markdown 拼回去 == 原文），所以 bake（`[N]` 绑定 / 数字守卫）照旧在切之前跑一遍。

规则：
- 只认行首 `## `（ATX 二级）；围栏（``` / ~~~）内不算；`###` 留在章内
- **层级回退**（tech-design §2.1 补充）：`##` 少于 2 个而 `###` ≥ 2 个 ⇒ 按 `###` 切——116 语料
  15% 的亲笔段是「第一行一个 `##` 当文档标题 + `###` 当章」；那个 `##` 留在它所在的块里
- 第一个章标题之前 = 导语（可空）；只有标题没正文的章并入下一章；导语只有标题行也并入第一章
- 只有一个 `##`（或没有 `##`、只有一个 `###`）⇒ 导语（若有）+ 这一章；整篇就是一章时单元素但 is_lead=False、带标题
- 没有任何章标题 ⇒ 整篇一段（与不切时逐字节相同）
- `Chapter.title` = 标题行去 `## `、去尾部块级图锚 / 加粗标记，给 read_report 与
  existing_sections 当标题，让 peer 按内容挑段
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_H2_RE = re.compile(r"^## +(.*?)\s*$")
_H3_RE = re.compile(r"^### +(.*?)\s*$")
_HEADING_RE = re.compile(r"^#{1,6} +(.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_FIGURE_ANCHOR_RE = re.compile(r"\s*\[figure:[A-Za-z0-9_]+\]\s*")
_TITLE_NOISE_RE = re.compile(r"[*_`#]+")
_TITLE_NORM_RE = re.compile(r"[\s\W_]+", re.UNICODE)


@dataclass(frozen=True)
class Chapter:
    markdown: str
    title: str
    is_lead: bool

    @property
    def is_empty(self) -> bool:
        return not self.markdown.strip()


def chapter_title(heading_line: str) -> str:
    """`## 品类结构：女装一家独大 [figure:sec_x]` → `品类结构：女装一家独大`（`###` 同样）。"""
    m = _HEADING_RE.match(heading_line)
    raw = m.group(1) if m else heading_line
    raw = _FIGURE_ANCHOR_RE.sub(" ", raw)
    raw = _TITLE_NOISE_RE.sub("", raw)
    return raw.strip()[:120]


def normalize_title(title: str) -> str:
    """续写守卫按章匹配用：去空白 / 标点 / 大小写。"""
    return _TITLE_NORM_RE.sub("", title or "").lower()


def _heading_only(lines: list[str]) -> bool:
    return not any(ln.strip() for ln in lines[1:])


def _lead_is_heading_only(lines: list[str]) -> bool:
    """导语里除空白外只有标题行（`## 文档标题` 那种）⇒ 不值一段。"""
    body = [ln for ln in lines if ln.strip()]
    return bool(body) and all(_HEADING_RE.match(ln.rstrip("\r\n")) for ln in body)


def _heading_lines(lines: list[str]) -> tuple[list[int], list[int]]:
    """围栏外的 `## ` / `### ` 行号。"""
    h2: list[int] = []
    h3: list[int] = []
    in_fence = False
    fence_mark = ""
    for i, ln in enumerate(lines):
        m = _FENCE_RE.match(ln)
        if m:
            mark = m.group(1)
            if not in_fence:
                in_fence, fence_mark = True, mark
            elif mark == fence_mark:
                in_fence = False
            continue
        if in_fence:
            continue
        bare = ln.rstrip("\r\n")
        if _H2_RE.match(bare):
            h2.append(i)
        elif _H3_RE.match(bare):
            h3.append(i)
    return h2, h3


def split_chapters(markdown: str) -> list[Chapter]:
    """整篇 → [导语?, 章...]。没有 `##` ⇒ 单元素（is_lead=True）。字节守恒。"""
    if not markdown:
        return [Chapter(markdown=markdown, title="", is_lead=True)]
    lines = markdown.splitlines(keepends=True)
    # 1) Mark unfenced chapter headings: ≥2 ## splits by ##; otherwise ≥2 ### splits by ### (level fallback).
    #    Sole ## also counts when no multichapter split: entire document one chapter (new-turn add-chapter shape),
    #    persisted with chapter title / style=insight, not introduction (dogfood §5.3).
    #    No ## and sole ### also counts: writer adding to ###-chapter document follows ### level
    #    (116 1.2.32 D1: ### repeat-purchase chapter became executive, ordered behind shell).
    h2, h3 = _heading_lines(lines)
    heads = h2 if len(h2) >= 2 else (h3 if len(h3) >= 2 else (h2 or h3))
    if not heads:
        return [Chapter(markdown=markdown, title="", is_lead=True)]
    # 2) Split: [0,heads[0]) introduction; each heading through next heading thereafter.
    bounds = [0, *heads, len(lines)]
    blocks: list[list[str]] = [lines[a:b] for a, b in zip(bounds[:-1], bounds[1:], strict=False)]
    lead_lines, chapter_blocks = blocks[0], blocks[1:]
    # 3) Heading-only chapter merges into next (take next content-bearing title); retain last without successor.
    merged: list[tuple[list[str], str]] = []
    carry: list[str] = []
    last = len(chapter_blocks) - 1
    for i, blk in enumerate(chapter_blocks):
        title = chapter_title(blk[0].rstrip("\r\n"))
        if _heading_only(blk) and i != last:
            carry = carry + blk
            continue
        merged.append((carry + blk, title))
        carry = []
    out: list[Chapter] = []
    lead_md = "".join(lead_lines)
    if lead_md.strip() and not _lead_is_heading_only(lead_lines):
        out.append(Chapter(markdown=lead_md, title="", is_lead=True))
    elif lead_md:
        # Introduction only whitespace / heading (`## Document title`): merge into first chapter, conserving bytes.
        merged[0] = (lead_lines + merged[0][0], merged[0][1])
    for blk, title in merged:
        out.append(Chapter(markdown="".join(blk), title=title, is_lead=False))
    return out


def assign_unverified(chapters: list[Chapter], unverified: list[str] | None) -> list[list[str]]:
    """bake 对整篇算出的未验证数字 → 按「值出现在哪章」分到各段；哪章都不含的落到第一段。
    比较时两边都去空白（bake 的形态是 `9.9亿`，正文可能写 `9.9 亿`）。"""
    per: list[list[str]] = [[] for _ in chapters]
    if not unverified or not chapters:
        return per
    flat = [re.sub(r"\s+", "", ch.markdown) for ch in chapters]
    for item in unverified:
        key = re.sub(r"\s+", "", str(item))
        idx = next((i for i, text in enumerate(flat) if key and key in text), 0)
        per[idx].append(item)
    return per


def first_heading_title(markdown: str) -> str:
    """正文里第一个围栏外的 `##` / `###` 标题行的标题（没有 ⇒ ""）。"""
    lines = (markdown or "").splitlines(keepends=True)
    h2, h3 = _heading_lines(lines)
    idx = min([*h2, *h3], default=None)
    return chapter_title(lines[idx].rstrip("\r\n")) if idx is not None else ""


def refresh_chapter_title(patch: dict, current: dict | None) -> dict:
    """段正文被整段改写时同步 `_meta.chapter_title`（dogfood §5.6：整章译成英文后 outline 仍是中文旧标题，
    下一轮按标题 reconcile 会对不上 ⇒ 当成新章追加）。只对本来就是章的段（已有 chapter_title）动手；
    patch 里的 `_meta` 与库里的浅合并，别把 composed_by / chapter 等其它子键顶掉。"""
    if not isinstance(patch, dict) or not isinstance(patch.get("markdown"), str):
        return patch
    cur_meta = (current or {}).get("_meta") if isinstance(current, dict) else None
    if not (isinstance(cur_meta, dict) and cur_meta.get("chapter_title")):
        return patch
    title = first_heading_title(patch["markdown"]) or str(cur_meta["chapter_title"])
    incoming = patch.get("_meta") if isinstance(patch.get("_meta"), dict) else {}
    out = dict(patch)
    out["_meta"] = {**cur_meta, **incoming, "chapter_title": title}
    return out

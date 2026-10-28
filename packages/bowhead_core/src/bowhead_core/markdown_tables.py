"""GFM 表格的共享判据 —— 切列 / 分隔行 / 表头单位。

**为什么单独一个模块**：核验侧（`report_writer_loop._collect_unverified`）和绑定侧
（`citation_marks.bind_to_numbered`）对「表格长什么样、某一列的单位是什么」必须给出
**逐字相同**的回答，否则同一个数字会出现「核验器看得见、binder 看不见」——
那正是 trust 百分比被系统性压低的根因（进得了分母、进不了分子）。

而 `report_writer_loop` 依赖 `citation_marks`，反向 import 会成环，所以判据不能住在
任何一侧，只能放在这里由两边共同 import。

★ 已知的同族历史缺陷（都是「两侧判据漂移」的不同症状，别再制造新的）：
  - 负号 lookbehind 一侧排了 CJK 一侧没排 ⇒ 数字静默消失；
  - `_THOUSAND_SEP` 的 `\\b` 在 CJK 前不成立 ⇒ `43,964万元` 绑不上；
  - 数字与单位之间的空格：核验侧允许、绑定侧不允许 ⇒ `108,350 万元` 绑不上；
  - 单位写在表头、格里是裸数字 ⇒ 核验侧靠本模块的 `header_units` 够得到，
    绑定侧此前完全够不到（116 全库 3,057 个，占表格数字三分之二）。
"""

from __future__ import annotations

import re

# GFM delimiter rows: **one or more** hyphens per cell are valid, with optional alignment colons and trailing pipe.
# ⚠ Do not tighten this to "at least three hyphens": that is **not the spec** (`|-|-|` / `|:-:|:-:|` are valid).
# Stricter matching prevents header recognition for the whole table ⇒ all bare numbers lose verification, worse than the missed cases it aims to prevent.
# Null-value data rows containing only `-` and delimiter rows are **syntactically indistinguishable**; the spec does not distinguish them either.
# The only reliable criterion is **position**: the delimiter can only be the second table row (see table_line_no on both sides).
# Still require a leading pipe: bare `---` is a horizontal rule; do not treat the preceding line as a header.
TABLE_SEP_RE = re.compile(r"^\|\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")

# Longer unit strings first; otherwise 「万元」 is consumed as 「万」, differing by a factor of 1e4.
HEADER_UNITS: tuple[str, ...] = ("亿元", "万元", "亿", "万", "%", "元", "个", "家", "pp", "倍")

# Header units must occur **after a delimiter and at the end** (in parentheses / after a slash / after whitespace), never by whole-cell substring matching:
# Otherwise 「国家」→ 家, 「单元」→ 元, 「万科营收」→ 万 (factor of 1e4), and 「元气森林」→ 元 are all false matches.
HEADER_UNIT_RE = re.compile(
    r"(?:^|[（(/／·\s])(亿元|万元|亿|万|%|元|个|家|pp|倍)\s*[）)]?\s*$",
)

# Pipes not escaped by a backslash are the actual table column delimiters.
UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


def split_row(line: str) -> list[str]:
    """`| a | b |` → ['a','b']（去掉首尾竖线，不 strip 单元格内容）。

    只在**未转义**的竖线处切：`\\|` 是单元格内的字面竖线，裸 `split("|")` 会把它当分隔符，
    列数凭空多一列 ⇒ 表头单位落到错误的列（例如把「万元」补给计数列）。
    前端 serializer 写单元格时正是把 `|` 转义成 `\\|`，往返越多越容易撞上。
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return UNESCAPED_PIPE_RE.split(s)


def header_units(header_line: str) -> list[str | None]:
    """从表头取每列单位：「营业额（万元）」→ 万元，「占比 %」→ %，「品牌」→ None。

    单位必须在**末尾且前面有分隔符**（见 `HEADER_UNIT_RE`）。整格子串匹配会把
    「国家」读成「家」、「单元」读成「元」、「万科营收」读成「万」（后者直接差 1e4）。
    """
    return [
        (m.group(1) if (m := HEADER_UNIT_RE.search(cell.strip())) else None)  # noqa: E999
        for cell in split_row(header_line)
    ]


def cell_spans(line: str) -> list[tuple[int, int]]:
    """表格行 → 每个单元格在**原行**里的 (start, end) 区间，与 `split_row` 同序等长。

    绑定侧需要「这个数字落在第几列」才能查表头单位，而 `split_row` 丢掉了位置信息。
    这里按同一套未转义竖线规则重算区间，保证两者逐列对齐。
    """
    s = line
    start = 0
    end = len(s)
    # Match split_row: skip leading/trailing pipes (an escaped trailing pipe does not count).
    lead = len(s) - len(s.lstrip())
    body = s.strip()
    if body.startswith("|"):
        start = lead + 1
    else:
        start = lead
    if body.endswith("|") and not body.endswith("\\|"):
        end = lead + len(body) - 1
    else:
        end = lead + len(body)
    if start > end:
        # Degenerate row (a single pipe): `split_row` returns `['']` here;
        # likewise return one empty span, or the lengths will differ.
        # `_bind_table_row` zips spans with header units; unequal lengths silently omit cells.
        return [(start, start)]
    spans: list[tuple[int, int]] = []
    cur = start
    for m in UNESCAPED_PIPE_RE.finditer(s, start, end):
        spans.append((cur, m.start()))
        cur = m.end()
    spans.append((cur, end))
    return spans

"""表格数字必须能进 verified 分子 —— trust 百分比的单边放开。

`_collect_unverified` 在 2026-08-10 取消跳过 `|` 行（表格数字进核验），但铸 `[N]` 的
binder 还跳着 ⇒ 表格数字**只能进 unverified 分母、永远进不了 verified 分子**。
`pct = verified/(verified+unverified)` 因此对表格多的报告系统性偏低：一张内容全对的表里
只要有一个 `_is_derivable` 覆盖不到的合计行（它有 a/b-1、a-b、a×frac，**唯独没有 a+b**），
pct 就能从 100 掉到 50 —— 报告没错，分数错了。

本文件钉三件事：
① 表格行能绑 `[N]`，且真的抬得动 pct（端到端过 `compute_report_trust`）；
② 安全面**没有**被这次放开削弱：模型伪造的表格 `[N]` 照旧剥干净；
③ ★ ②之所以成立，靠的是**顺序**（strip 跑在所有铸造者之前），不再是行型 ——
   所以顺序本身要有守卫，否则谁把 binder 挪到 strip 之前，谁就同时开了洞而测试全绿。
"""

from __future__ import annotations

import ast
from pathlib import Path

from bowhead_core.citation_marks import bind_to_numbered, strip_untrusted_numbered
from bowhead_core.report_trust import compute_report_trust
from bowhead_core.sources_resolver import TurnSourcesRegistry


def _reg_with(*ids: str) -> TurnSourcesRegistry:
    reg = TurnSourcesRegistry()
    for i in ids:
        reg.register_source(i, tool="bi_query", query=f"q-{i}")
    return reg


def test_table_numbers_can_earn_a_numbered_citation() -> None:
    reg = _reg_with("tl_a")
    md = "\n".join(
        [
            "头部品牌营业额如下。",
            "",
            "| 品牌 | 营业额 |",
            "| --- | --- |",
            "| 金辉福 | 9627万元 |",
            "| 六福 | 3210万元 |",
        ]
    )

    out = bind_to_numbered(
        md,
        value_to_source={9627e4: "tl_a", 3210e4: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )

    assert "| 金辉福 | 9627万元 [1] |" in out
    assert "| 六福 | 3210万元 [1] |" in out
    # Separator row contains no numbers and must remain untouched.
    assert "| --- | --- |" in out


def test_a_correct_table_no_longer_scores_50_percent() -> None:
    """端到端复现被修的那条：内容全对的报告不该因为一个合计行掉到 50%。

    合计行 73,032 万元 = 三者之和的忠实舍入，但 `_is_derivable` 没有 `a+b` ⇒ 进 unverified。
    改前表格数字进不了分子 ⇒ verified=1（正文那处）/ unverified=1 ⇒ pct=50。
    改后表格里可绑的数字也计入分子 ⇒ pct 回到合理区间。
    """
    reg = _reg_with("tl_a")
    md = "\n".join(
        [
            "全省营业额 9627万元。",
            "",
            "| 品牌 | 营业额 |",
            "| --- | --- |",
            "| 金辉福 | 4396万元 |",
            "| 六福 | 1596万元 |",
            "| 老庙 | 1311万元 |",
        ]
    )
    bound = bind_to_numbered(
        md,
        value_to_source={9627e4: "tl_a", 4396e4: "tl_a", 1596e4: "tl_a", 1311e4: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )

    sections = [
        {"kind": "narrative", "markdown": bound, "unverified_numbers": ["73032万元"]},
        {"kind": "sources", "entries": [{"id": "tl_a", "title": "BI 查询"}]},
    ]
    stats = compute_report_trust(sections)

    assert stats.verified == 4, f"表格里的三处没进分子：verified={stats.verified}"
    assert stats.unverified == 1
    assert stats.verified_pct == 80, f"pct={stats.verified_pct}（改前这份是 50）"


def test_thousand_separated_cells_can_bind() -> None:
    """`43,964万元` 这种写法必须能绑 —— 它是 BI 表格最标准的数字形态。

    `_THOUSAND_SEP` 原本用 `\\b` 收尾，而 CJK 算 `\\w` ⇒ 千分位后面紧跟单位时逗号去不掉
    ⇒ 解析失败 ⇒ 绑不上。放开 `|` 行之后如果不修这条，表格里最常见的一类数字
    照旧进不了 verified 分子 —— 等于这条修复对它们无效。
    """
    reg = _reg_with("tl_a")
    md = "| 品牌 | 营业额 |\n| --- | --- |\n| 金辉福 | 43,964万元 |"

    out = bind_to_numbered(
        md,
        value_to_source={4.3964e8: "tl_a"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )

    assert "43,964万元 [1]" in out


def test_forged_table_citations_are_still_stripped() -> None:
    """安全面反向锁：放开 binder **不能**顺带放过模型自己写的表格 `[N]`。"""
    forged = "| 指标 | 值 |\n|---|---|\n| 营收 | 100亿 [1] |\n| 利润 | 20亿 [2] |"

    out = strip_untrusted_numbered(forged)

    assert "[1]" not in out and "[2]" not in out, f"伪造的表格引用漏出来了：{out}"


def test_forged_table_cannot_inflate_pct_end_to_end() -> None:
    """零工具调用的 50 行表格全写 `[1]`，剥完之后 verified 必须是 0。"""
    rows = "\n".join(f"| 品牌{i} | {1000 + i}亿 [1] |" for i in range(50))
    md = f"| 品牌 | 值 |\n|---|---|\n{rows}"

    sections = [
        {"kind": "narrative", "markdown": strip_untrusted_numbered(md)},
        {"kind": "sources", "entries": [{"id": "tl_a", "title": "BI 查询"}]},
    ]
    stats = compute_report_trust(sections)

    assert stats.verified == 0, f"伪造表格刷出 verified={stats.verified}"
    assert stats.verified_pct != 100


def test_strip_runs_before_every_minter_in_both_writer_paths() -> None:
    """★ 结构守卫：`strip_untrusted_numbered` 必须跑在 binder / `_resolve_cites` **之前**。

    表格行不再被 binder 跳过之后，「表格里的 [N] 必是伪造」这条**行型**判据彻底作废 ——
    区分真假引用的唯一依据变成**顺序**：strip 执行的那一刻全文不存在任何合法裸 `[N]`。
    谁把 binder 挪到 strip 之前，上面那两条安全用例**仍然全绿**（它们直接调函数，
    不经过 writer 的编排顺序），而生产上伪造的 `[N]` 会当场混过去。所以顺序要单独钉。
    """
    path = Path(__file__).resolve().parents[1] / "src/bowhead_core/report_writer_loop.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def own_calls(fn: ast.AST) -> list[tuple[str, int]]:
        """该函数**自己**的调用（不下钻到嵌套 def —— 嵌套 def 会被单独检查一遍）。"""
        found: list[tuple[str, int]] = []
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            node = stack.pop()
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                found.append((name, node.lineno))
            stack.extend(ast.iter_child_nodes(node))
        return found

    MINTERS = {"_apply_citation_pass", "_resolve_cites", "bind_to_numbered"}
    checked_names: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if fn.name in MINTERS:
            continue  # Minters themselves must not strip; the constraint applies to their callers.
        calls = own_calls(fn)
        mints = [ln for name, ln in calls if name in MINTERS]
        if not mints:
            continue
        strips = [ln for name, ln in calls if name == "strip_untrusted_numbered"]
        assert strips, f"{fn.name} 铸 [N] 却没先 strip —— 模型伪造的 [N] 会被当成真引用"
        assert min(strips) < min(mints), f"{fn.name}: strip 在第 {min(strips)} 行、铸造在第 {min(mints)} 行 —— 顺序反了"
        checked_names.append(fn.name)

    # delivery-surface M-B consolidated minting into **one seam**: direct-output and tool-loop pipelines
    # now share `bake_report_markdown` (called uniformly by write_report handlers). The guard's
    # completeness assertion changes from ≥2 sites to requiring the canonical seam. New minting sites
    # still face the per-function ordering assertions above; renaming/moving the seam fails loudly here.
    assert "bake_report_markdown" in checked_names, f"正典铸造 seam 不在册（检到 {checked_names}）——守卫失效，先修守卫"


# ── Header-unit-aware binding (the two gaps exposed by 116 dogfood) ──────────────────


def _bound(md: str, v2s: dict) -> str:
    reg = _reg_with("tl_a")
    return bind_to_numbered(md, value_to_source=v2s, sources=reg.sources, id_to_index=reg.id_to_index)


def test_bare_cell_binds_via_header_unit() -> None:
    """「单位写在表头、格里是裸数字」是产线**最常见**的表格形态（3,057 个，占 2/3）。

    116 dogfood 实证：让写手「用表格并带单位」，它默认就把单位放表头、格里放 `108,350`。
    `_AUTOWRAP_PLAIN_RAW` 要求数字自带量级才算显著数字 ⇒ 这一类此前**永远绑不上**，
    只能进 unverified 分母 —— 「binder 纳入 `|` 行」那条修复对它们等于没做。
    """
    md = "| 门店 | 净营业额（万元） |\n| --- | --- |\n| 东湖街 | 108,350 |"

    out = _bound(md, {1.0835e9: "tl_a"})

    assert "| 东湖街 | 108,350 [1] |" in out


def test_header_unit_is_used_for_resolution_only_never_written_into_text() -> None:
    """只拿表头单位去**解析**，绝不把它写进正文 —— 引用绑定不该改写用户可见文本。"""
    md = "| 门店 | 净营业额（万元） |\n| --- | --- |\n| 东湖街 | 108,350 |"

    out = _bound(md, {1.0835e9: "tl_a"})

    assert "108,350万元" not in out, "把表头单位写进单元格了"
    assert "108,350 [1]" in out


def test_space_between_number_and_unit_binds() -> None:
    """`108,350 万元` —— 核验侧的 `_MARK_NUM_RE` 一直允许空白，绑定侧此前不允许。

    两侧判据漂移的后果永远是同一个方向：进得了分母、进不了分子。
    116 dogfood 第二轮写手产出的正是这种带空格的写法。
    """
    md = "| 门店 | 净营业额 |\n| --- | --- |\n| 东湖街 | 108,350 万元 |"

    assert "108,350 万元 [1]" in _bound(md, {1.0835e9: "tl_a"})


def test_column_without_a_header_unit_is_not_bound() -> None:
    """反向锁：表头没单位的列，裸数字不得凭空绑上（否则排名/序号会被当业务数字）。"""
    md = "| 门店 | 名次 |\n| --- | --- |\n| 东湖街 | 108350 |"

    out = _bound(md, {1.0835e9: "tl_a", 108350.0: "tl_a"})

    assert "[1]" not in out


def test_header_row_never_gets_citations() -> None:
    """反向锁：表头行不承载业务数字，绑上去就是排版噪音。

    （分隔行不单独断言：它按定义只含 `-` `:` `|` 空格，`_bind_line` 在上面**恒为空操作**，
    写成断言也恒真、逮不住任何回归 —— 与其留一条假绿的锁，不如把这句话写清楚。）
    """
    # ★ The header must contain a number `_bind_line` **recognizes** (with its own unit); otherwise
    # mutating it to bind headers is a no-op here: regular passes already ignore bare numbers, falsely passing the test.
    md = "| 门店 | 峰值108350万元 |\n| --- | --- |\n| 东湖街 | 5 |"

    lines = _bound(md, {1.0835e9: "tl_a"}).split("\n")

    assert "[1]" not in lines[0], f"表头行被绑了：{lines[0]}"
    assert "[1]" not in lines[1], f"分隔行被绑了：{lines[1]}"


def test_cell_number_with_its_own_unit_is_not_double_bound() -> None:
    """自带单位的格子归常规 pass 管；表头单位那条路不能重复绑出两个 [N]。"""
    md = "| 门店 | 净营业额（万元） |\n| --- | --- |\n| 东湖街 | 108350万元 |"

    out = _bound(md, {1.0835e9: "tl_a"})

    assert out.count("[1]") == 1, f"重复绑定：{out}"


def test_writer_written_citation_in_a_cell_is_not_double_bound() -> None:
    """写手已经显式引用过的数字不重复绑（#11 的既有契约，表格路同样要守）。"""
    md = "| 门店 | 净营业额（万元） |\n| --- | --- |\n| 东湖街 | 108,350 [1] |"

    out = _bound(md, {1.0835e9: "tl_a"})

    assert out.count("[1]") == 1, f"重复绑定：{out}"


def test_escaped_pipe_does_not_shift_the_column_to_unit_mapping() -> None:
    """`\\|` 是格内字面竖线；按裸 split 切列会让表头单位落到错误的列。

    绑定侧与核验侧共用 `markdown_tables.split_row` / `cell_spans`，这条钉住它们同序对齐。
    """
    md = "| 名称 \\| 别名 | 净营业额（万元） |\n| --- | --- |\n| 东湖街 \\| 东街 | 108,350 |"

    out = _bound(md, {1.0835e9: "tl_a"})

    assert "108,350 [1]" in out, f"表头单位错列了：{out}"


def test_header_unit_table_and_scale_table_stay_in_sync() -> None:
    """`HEADER_UNITS` 与 `_HEADER_UNIT_TO_SCALE` 必须一一对应。

    `_bind_bare_in_cell` 里那条 `scale is None` 的早退在今天是**不可达**的（`header_units`
    只会吐出前者里的成员）。把「两张表同步」变成一条会红的断言，比留一个测不到的
    防御分支诚实 —— 也正好挡住本轮一直在修的那类「两处判据漂移」。
    """
    from bowhead_core.citation_marks import _HEADER_UNIT_TO_SCALE
    from bowhead_core.markdown_tables import HEADER_UNITS

    assert set(HEADER_UNITS) == set(_HEADER_UNIT_TO_SCALE), (
        f"漏映射: {set(HEADER_UNITS) - set(_HEADER_UNIT_TO_SCALE)}; 多余: {set(_HEADER_UNIT_TO_SCALE) - set(HEADER_UNITS)}"
    )


def test_header_units_do_not_leak_across_tables_separated_by_prose() -> None:
    """表格 → **紧邻的正文行** → 另一张表：第一张的表头单位不得漏给第二张。

    ★ 两处重置（空行 / 非表格行）会**互相遮蔽**：fixture 里正文前后留了空行时，
    摘掉任意一处 mutation 都全绿 —— 因为另一处先重置了。所以这里**不留空行**，
    只让「非表格行」那条重置生效，它才真正被钉住。
    """
    md = "\n".join(
        [
            "| 门店 | 净营业额（万元） |",
            "| --- | --- |",
            "| 东湖街 | 108,350 |",
            "以下是另一张表：",
            "| 门店 | 名次 |",
            "| --- | --- |",
            "| 东湖街 | 108350 |",
        ]
    )

    lines = _bound(md, {1.0835e9: "tl_a"}).split("\n")

    assert "108,350 [1]" in lines[2], f"第一张表没绑上：{lines[2]}"
    assert "[1]" not in lines[6], f"第二张表沿用了上一张的表头单位：{lines[6]}"


def test_header_units_do_not_leak_across_tables_separated_by_a_blank_line() -> None:
    """同上，但两张表之间**只隔一个空行** —— 单独钉住「空行重置」那条。"""
    md = "\n".join(
        [
            "| 门店 | 净营业额（万元） |",
            "| --- | --- |",
            "| 东湖街 | 108,350 |",
            "",
            "| 门店 | 名次 |",
            "| --- | --- |",
            "| 东湖街 | 108350 |",
        ]
    )

    lines = _bound(md, {1.0835e9: "tl_a"}).split("\n")

    assert "108,350 [1]" in lines[2], f"第一张表没绑上：{lines[2]}"
    assert "[1]" not in lines[6], f"第二张表沿用了上一张的表头单位：{lines[6]}"


def test_header_units_do_not_leak_across_a_code_fence() -> None:
    """代码围栏也必须重置表格状态 —— 单独钉住 fence 那条重置。

    三处重置（fence / 空行·跳过行 / 非表格行）在同一份 fixture 里会互相遮蔽，
    所以每条各有一个只让它生效的用例。
    """
    md = "\n".join(
        [
            "| 门店 | 净营业额（万元） |",
            "| --- | --- |",
            "| 东湖街 | 108,350 |",
            # ★ Use an **empty fence**: any interior line takes the skip branch, which also resets state;
            # these redundant resets mask each other, so removing the fence reset still passes. An empty fence isolates it.
            "```",
            "```",
            "| 门店 | 名次 |",
            "| --- | --- |",
            "| 东湖街 | 108350 |",
        ]
    )

    lines = _bound(md, {1.0835e9: "tl_a"}).split("\n")

    assert "108,350 [1]" in lines[2], f"第一张表没绑上：{lines[2]}"
    assert "[1]" not in lines[7], f"围栏之后沿用了上一张表的表头单位：{lines[7]}"


def test_cell_spans_stays_aligned_with_split_row_on_degenerate_rows() -> None:
    """两者必须**同序等长**：`_bind_table_row` 用 zip 把区间与表头单位配对，
    长度不齐会静默漏掉单元格。

    ★ 自查逮到：整行只有一根竖线时 `split_row` 给 `['']` 而 `cell_spans` 给 `[]`。
    """
    from bowhead_core.markdown_tables import cell_spans, split_row

    for line in ["  | a | b |", "a | b", "|  |  |", "|", "||", "| a |", r"| a \| b | c |"]:
        cells, spans = split_row(line), cell_spans(line)
        assert len(cells) == len(spans), f"{line!r}: {len(cells)} cells vs {len(spans)} spans"
        assert all(line[a:b] == c for c, (a, b) in zip(cells, spans, strict=True)), line

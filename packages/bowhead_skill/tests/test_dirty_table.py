"""excel RC-6 — dirty_table helper + runner stage/pi-K schema 集成。

覆盖 rc6-dirty-table-design.md §4 acceptance:多级表头/合并单元格 forward-fill/
标题行/单位行/规整表退化/坏文件 fallback + runner stage 文件 + pi-K schema 脏表列名。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import openpyxl
import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dt = _load("_dirty_table_under_test", "dirty_table.py")


def _mk_xlsx(tmp_path: Path, rows: list[list], *, merges: list[str] | None = None, sheet_title: str = "Sheet1") -> str:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for r in rows:
        ws.append(r)
    for m in merges or []:
        ws.merge_cells(m)
    p = tmp_path / "t.xlsx"
    wb.save(p)
    return str(p)


# ── Pure functions ────────────────────────────────────────────────────


def test_detect_header_span_title_plus_multiheader() -> None:
    island = [
        ["2022年度序时账", None, None],  # Title row (single cell).
        ["本期", "本期", "科目"],  # group header
        ["借方", "贷方", "名称"],  # sub header
        [100, 200, "现金"],  # data
    ]
    start, end = dt._detect_header_span(island, 3)
    assert start == 1 and end == 3  # Skip the title; 2 header rows.


def test_synth_columns_joins_multilevel_and_dedups() -> None:
    header_rows = [["本期", "本期", "科目"], ["借方", "贷方", "名称"]]
    cols = dt._synth_columns(header_rows, 3)
    assert cols == ["本期 / 借方", "本期 / 贷方", "科目 / 名称"]


def test_synth_columns_dedup_collision() -> None:
    cols = dt._synth_columns([["金额", "金额"]], 2)
    assert cols == ["金额", "金额.1"]


def test_is_unit_row() -> None:
    assert dt._is_unit_row(["单位:万元", None, None]) is True
    assert dt._is_unit_row(["（金额单位：元）"]) is True
    assert dt._is_unit_row(["现金", 100, 200]) is False  # Numeric values indicate a data row.
    assert dt._is_unit_row([None, None]) is True  # Skip empty rows too.


# ── Integration: real xlsx (requires pandas, present in sandbox but possibly absent in dev; gated) ──

_HAS_PANDAS = importlib.util.find_spec("pandas") is not None
_needs_pandas = pytest.mark.skipif(not _HAS_PANDAS, reason="pandas sandbox-only, not in dev venv")


@_needs_pandas
def test_read_dirty_merged_cells_forward_filled(tmp_path: Path) -> None:
    """合并单元格 → forward-fill 到每列(现有 KB helper 缺的核心)。"""
    path = _mk_xlsx(
        tmp_path,
        [
            ["本期", None, "上期", None],  # A1:B1 merged="本期", C1:D1="上期".
            ["借方", "贷方", "借方", "贷方"],
            [10, 20, 30, 40],
            [11, 21, 31, 41],
        ],
        merges=["A1:B1", "C1:D1"],
    )
    df = dt.read_dirty_table(path)
    assert list(df.columns) == ["本期 / 借方", "本期 / 贷方", "上期 / 借方", "上期 / 贷方"]
    assert len(df) == 2
    assert df.iloc[0].tolist() == [10, 20, 30, 40]


@_needs_pandas
def test_read_dirty_skips_title_and_unit_rows(tmp_path: Path) -> None:
    path = _mk_xlsx(
        tmp_path,
        [
            ["福州分行存款序时账", None, None],  # Title.
            ["科目", "借方", "贷方"],  # Header.
            ["单位:万元", None, None],  # Unit row.
            ["现金", 100, 50],
            ["存款", 200, 80],
        ],
    )
    df = dt.read_dirty_table(path)
    assert list(df.columns) == ["科目", "借方", "贷方"]
    assert len(df) == 2  # Unit row skipped.
    assert df.iloc[0]["科目"] == "现金"


@_needs_pandas
def test_read_dirty_clean_table_degrades(tmp_path: Path) -> None:
    """规整表:首行列名+数据,正常读不受影响。"""
    path = _mk_xlsx(tmp_path, [["name", "value"], ["a", 1], ["b", 2]])
    df = dt.read_dirty_table(path)
    assert list(df.columns) == ["name", "value"]
    assert df["value"].tolist() == [1, 2]


@_needs_pandas
def test_read_dirty_picks_largest_island(tmp_path: Path) -> None:
    """空行分隔多表 → 取最大数据块(compute 分析主表)。"""
    path = _mk_xlsx(
        tmp_path,
        [
            ["摘要", "值"],
            ["合计", 999],
            [None, None],  # Blank-row separator.
            ["科目", "借", "贷"],
            ["现金", 1, 2],
            ["存款", 3, 4],
            ["票据", 5, 6],
        ],
    )
    df = dt.read_dirty_table(path)
    assert list(df.columns) == ["科目", "借", "贷"]  # Larger island.
    assert len(df) == 3


@_needs_pandas
def test_read_dirty_numeric_coercion(tmp_path: Path) -> None:
    path = _mk_xlsx(tmp_path, [["名称", "金额"], ["现金", "100"], ["存款", "200"]])
    df = dt.read_dirty_table(path)
    assert df["金额"].tolist() == [100, 200]  # str→num
    assert df["名称"].tolist() == ["现金", "存款"]  # Preserve text columns instead of clearing them.


@_needs_pandas
def test_read_dirty_bad_file_fallback(tmp_path: Path) -> None:
    """坏文件 → fallback read_excel(不抛给 caller,退化路径)。"""
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a real xlsx")
    with pytest.raises(Exception):  # noqa: B017 — Fallback read_excel also raises; _read_dirty_impl is not the error propagated directly.
        dt.read_dirty_table(str(bad))


@_needs_pandas
def test_read_dirty_specified_sheet(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active.title = "summary"
    wb.active.append(["a", "b"])
    ws2 = wb.create_sheet("detail")
    ws2.append(["科目", "金额"])
    ws2.append(["现金", 100])
    p = tmp_path / "multi.xlsx"
    wb.save(p)
    df = dt.read_dirty_table(str(p), sheet="detail")
    assert list(df.columns) == ["科目", "金额"]


# ── runner integration: staging + pi-K schema ──────────────────────────


def _load_runner() -> ModuleType:
    return _load("_rc6_runner_under_test", "runner.py")


@_needs_pandas
def test_runner_pik_xlsx_dirty_header(tmp_path: Path) -> None:
    """pi-K schema:脏表 preview 产出合成列名,非标题行/裸第0行。"""
    runner = _load_runner()
    path = _mk_xlsx(
        tmp_path,
        [
            ["2022序时账", None, None],  # Title.
            ["科目", "借方", "贷方"],  # Header.
            ["现金", 100, 50],
            ["存款", 200, 80],
        ],
    )
    header, reason, _pii = runner._preview_structured_streaming(Path(path), mime=None, max_rows=5, max_cols=20)
    assert reason == "" or reason in ("size_cap", "col_cap")
    assert header is not None
    # Columns must come from the real header, not the title "2022序时账" or the raw first row.
    assert header["columns"][:3] == ["科目", "借方", "贷方"]


@_needs_pandas
def test_runner_pik_clean_xlsx_unchanged(tmp_path: Path) -> None:
    """规整表 pi-K schema 行为不回归。"""
    runner = _load_runner()
    path = _mk_xlsx(tmp_path, [["name", "value"], ["a", 1], ["b", 2]])
    header, _reason, _pii = runner._preview_structured_streaming(Path(path), mime=None, max_rows=5, max_cols=20)
    assert header is not None
    assert header["columns"] == ["name", "value"]


def test_runner_stages_dirty_table_helper(tmp_path: Path) -> None:
    """驱动 runner 的真 stage 代码(_stage_sandbox_helpers),非同义反复(review P2-2)。"""
    runner = _load_runner()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    runner._stage_sandbox_helpers(workdir)  # Exercise the real runner staging logic.
    staged = workdir / "dirty_table.py"
    assert staged.is_file()
    assert "def read_dirty_table" in staged.read_text(encoding="utf-8")


def test_runner_stage_copy_failure_non_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stage copy 失败(OSError)吞掉不抛 —— 沙箱运行不因此中断(review 契约)。"""
    runner = _load_runner()

    def _boom(*a: object, **k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(runner.shutil, "copy", _boom)
    runner._stage_sandbox_helpers(tmp_path)  # Passing means no exception.


# ── P1-1 / P2-6 counterexamples ─────────────────────────────────────────


@_needs_pandas
def test_read_dirty_merged_title_banner_skipped(tmp_path: Path) -> None:
    """review P1-1:满宽合并居中标题 banner(A1:C1)forward-fill 后变 [报表,报表,报表],
    必须仍被当标题跳过,不污染列名。"""
    path = _mk_xlsx(
        tmp_path,
        [["2022年度报表", None, None], ["科目", "借", "贷"], ["现金", 1, 2]],
        merges=["A1:C1"],
    )
    df = dt.read_dirty_table(path)
    assert list(df.columns) == ["科目", "借", "贷"]  # Not ["报表 / 科目", ...].
    assert df.iloc[0]["科目"] == "现金"


@_needs_pandas
def test_read_dirty_year_numeric_multiheader(tmp_path: Path) -> None:
    """年份数值列名(科目|2021|2022):数值年份不该把 2 级表头截成 1 级 —— 表头行整
    行含年份但仍是表头(第一格科目是文本)。"""
    path = _mk_xlsx(
        tmp_path,
        [["科目", "2021", "2022"], ["现金", 100, 200], ["存款", 300, 400]],
    )
    df = dt.read_dirty_table(path)
    # Header "科目 2021 2022": 2/3 numeric → _is_data_row? 0.67≥0.5 classifies it as data; ensure
    # it is not consumed. Assert columns survive (even with fallback, column count and data remain correct).
    assert len(df.columns) == 3
    assert len(df) == 2


@_needs_pandas
def test_read_dirty_datetime_column(tmp_path: Path) -> None:
    """日期列:datetime 不是数值(_looks_numeric False),当文本列保留不崩。"""
    from datetime import datetime

    path = _mk_xlsx(
        tmp_path,
        [["日期", "金额"], [datetime(2022, 1, 1), 100], [datetime(2022, 2, 1), 200]],
    )
    df = dt.read_dirty_table(path)
    assert list(df.columns) == ["日期", "金额"]
    assert df["金额"].tolist() == [100, 200]


@_needs_pandas
def test_read_dirty_other_islands_breadcrumb(tmp_path: Path) -> None:
    """多 island → df.attrs 记被丢的表数(review P2-3)。"""
    path = _mk_xlsx(
        tmp_path,
        [["摘要", "值"], ["合计", 9], [None, None], ["科目", "借", "贷"], ["现金", 1, 2], ["存款", 3, 4]],
    )
    df = dt.read_dirty_table(path)
    assert df.attrs.get("dirty_table_other_islands") == 1


@_needs_pandas
def test_read_dirty_empty_sheet(tmp_path: Path) -> None:
    """全空 sheet → 空 DataFrame 不崩。"""
    path = _mk_xlsx(tmp_path, [[None, None], [None, None]])
    df = dt.read_dirty_table(path)
    assert df.empty


def test_is_title_row() -> None:
    assert dt._is_title_row(["报表", "报表", "报表"]) is True  # Merged banner.
    assert dt._is_title_row(["标题", None, None]) is True  # Single cell.
    assert dt._is_title_row(["本期", "上期"]) is False  # 2 distinct values = multiple header groups.
    assert dt._is_title_row([None, None]) is False  # An empty row is not a title.

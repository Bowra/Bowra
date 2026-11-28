"""The sandbox reads Excel's East Asian built-in date formats as dates (spike: office-inline-edit-2026-11-26 §3.5).

The AI reads workbooks with pandas and openpyxl inside python_sandbox. A zh-CN 「2026年11月」 cell is
46327 with built-in format ID 57; openpyxl did not know the ID and both returned 46327. The scaffold's
L6 hook and the runner's input preview mark these IDs as dates.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import openpyxl

_RUNNER = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("_bh_python_sandbox_runner_east_asian_dates", _RUNNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _workbook(path: Path, format_id: int = 57) -> None:
    """A header and 46327 styled with the built-in `format_id` (no pattern stored, as Excel writes it)."""
    wb = openpyxl.Workbook()
    wb.active["A1"] = "月份"
    wb.active["A2"] = 46327
    wb.active["A2"].number_format = "0.000"
    raw = io.BytesIO()
    wb.save(raw)
    source = zipfile.ZipFile(io.BytesIO(raw.getvalue()))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/styles.xml":
                text = re.sub(r'<numFmts count="1"><numFmt numFmtId="164" formatCode="[^"]*"\s*/></numFmts>', "", data.decode())
                data = text.replace('numFmtId="164"', f'numFmtId="{format_id}"').encode()
            target.writestr(item, data)


def _run_user_code(tmp_path: Path, code: str) -> str:
    (tmp_path / "input.json").write_text(json.dumps({"inputs": {}}), encoding="utf-8")
    (tmp_path / "user.py").write_text(_load_runner()._SCAFFOLD + code, encoding="utf-8")
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(tmp_path / "user.py")], capture_output=True, text=True, check=False, cwd=str(tmp_path), timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_openpyxl_in_user_code_reads_the_date(tmp_path: Path) -> None:
    _workbook(tmp_path / "data.xlsx")
    out = _run_user_code(
        tmp_path,
        "import openpyxl\n"
        "ws = openpyxl.load_workbook('data.xlsx', read_only=True, data_only=True).active\n"
        "print(list(ws.iter_rows(values_only=True))[1][0])\n",
    )
    assert out == "2026-11-01 00:00:00"


def test_pandas_in_user_code_reads_the_date(tmp_path: Path) -> None:
    _workbook(tmp_path / "data.xlsx", format_id=31)
    out = _run_user_code(tmp_path, "import pandas as pd\nprint(pd.read_excel('data.xlsx').iloc[0, 0])\n")
    assert out == "2026-11-01 00:00:00"


def test_a_run_that_opens_no_workbook_does_not_import_openpyxl(tmp_path: Path) -> None:
    out = _run_user_code(tmp_path, "import sys\nprint('openpyxl' in sys.modules)\n")
    assert out == "False"


def test_the_runner_input_preview_reads_the_date(tmp_path: Path) -> None:
    runner = _load_runner()
    _workbook(tmp_path / "data.xlsx")
    runner._read_east_asian_dates()
    ws = openpyxl.load_workbook(tmp_path / "data.xlsx", read_only=True, data_only=True).active
    assert list(ws.iter_rows(values_only=True))[1][0].isoformat() == "2026-11-01T00:00:00"


def test_the_three_copies_of_the_rule_name_the_same_ids() -> None:
    from bowhead_core.xlsx_dates import EAST_ASIAN_DATE_FORMAT_IDS

    runner = _load_runner()
    assert runner._EAST_ASIAN_DATE_FORMAT_IDS == EAST_ASIAN_DATE_FORMAT_IDS
    assert "frozenset((*range(27, 37), *range(50, 59)))" in runner._SCAFFOLD

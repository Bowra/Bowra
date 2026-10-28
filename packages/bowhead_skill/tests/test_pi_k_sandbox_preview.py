"""Sprint π Phase C — Item K — python_sandbox INPUTS schema injection.

Tests `_preview_structured_streaming` + `_materialize_inputs` extension +
`tools.py` gate + Layer 2 metric drain.

Per tech-design-r2 §K (Layer 1 only; Layer 2 dropped per Eng P0-3 except
metric drain via runner sidecar).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────
# Load runner.py as a module (lives in catalog/python_sandbox/scripts/, not
# a normal python package).
# ─────────────────────────────────────────────────────────
_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_pi_k_runner_under_test",
        _RUNNER_PATH,
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_pi_k_runner_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _load_runner()


# ═════════════════════════════════════════════════════════
# K.2 — streaming preview shape
# ═════════════════════════════════════════════════════════


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def test_k_csv_preview_extracts_columns_and_sample(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    """test #1: CSV in → INPUTS contains columns + 5-row sample."""
    csv_path = tmp_path / "in.csv"
    _write_csv(csv_path, ["a", "b", "c"], [[i, i * 2, "x"] for i in range(1, 6)])
    preview, reason, _pii = runner._preview_structured_streaming(
        csv_path,
        mime="text/csv",
    )
    assert preview is not None
    assert preview["columns"] == ["a", "b", "c"]
    assert len(preview["sample_first_5"]) == 5
    assert preview["sample_first_5"][0] == {"a": "1", "b": "2", "c": "x"}
    assert preview["preview_truncated"] is False
    assert reason == ""


def test_k_csv_width_cap_20_cols(runner: ModuleType, tmp_path: Path) -> None:
    """test #2: 50-col CSV → ≤20 cols + _more_columns_count=30."""
    cols = [f"c{i}" for i in range(50)]
    _write_csv(tmp_path / "wide.csv", cols, [list(range(50))])
    preview, reason, _ = runner._preview_structured_streaming(
        tmp_path / "wide.csv",
        mime="text/csv",
    )
    assert preview is not None
    assert len(preview["columns"]) == 20
    assert preview["_more_columns_count"] == 30
    assert preview["preview_truncated"] is True
    assert reason == "col_cap"


def test_k_csv_row_height_cap_5(runner: ModuleType, tmp_path: Path) -> None:
    """test #3: 100-row CSV → sample 5 rows. (row_count for CSV stays None
    because counting all rows is not free per K.2.)
    """
    _write_csv(
        tmp_path / "tall.csv",
        ["a"],
        [[i] for i in range(100)],
    )
    preview, _r, _ = runner._preview_structured_streaming(
        tmp_path / "tall.csv",
        mime="text/csv",
    )
    assert preview is not None
    assert len(preview["sample_first_5"]) == 5
    # CSV: row_count is intentionally None (no cheap way to get full count
    # without scanning the file). Parquet/xlsx report row_count.
    assert preview["row_count"] is None


def test_k_cell_string_cap_200(runner: ModuleType, tmp_path: Path) -> None:
    """test #4: 500-char cell → truncated to 200 + ellipsis."""
    long_val = "x" * 500
    _write_csv(tmp_path / "big_cell.csv", ["a", "b"], [[long_val, "ok"]])
    preview, _r, _ = runner._preview_structured_streaming(
        tmp_path / "big_cell.csv",
        mime="text/csv",
    )
    assert preview is not None
    cell = preview["sample_first_5"][0]["a"]
    assert len(cell) == 201  # 200 + '…' (1 char)
    assert cell.endswith("…")
    assert preview["sample_first_5"][0]["b"] == "ok"


def test_k_total_2kb_cap(runner: ModuleType, tmp_path: Path) -> None:
    """test #5: wide stringified preview > 2KB → drop sample, keep columns."""
    # 15 cols × cell_len 190 chars (under 200 cap) × 5 rows ≈ 14KB → blow cap
    cols = [f"col_{i:02d}" for i in range(15)]
    rows = [["y" * 190] * 15 for _ in range(5)]
    _write_csv(tmp_path / "fat.csv", cols, rows)
    preview, reason, _ = runner._preview_structured_streaming(
        tmp_path / "fat.csv",
        mime="text/csv",
    )
    assert preview is not None
    assert reason == "size_cap"
    # All sample dropped to fit cap (keep columns + dtypes only)
    assert preview["sample_first_5"] == []
    assert preview["preview_truncated"] is True
    # Schema still present
    assert preview["columns"] == cols


def test_k_csv_100mb_streaming_rss(runner: ModuleType, tmp_path: Path) -> None:
    """test #6: streaming preview must not load 100MB CSV into RAM."""
    psutil = pytest.importorskip("psutil")
    big = tmp_path / "big.csv"
    # Generate ~100MB CSV without using pandas
    with big.open("w", encoding="utf-8") as f:
        f.write("a,b,c,d,e\n")
        line = "abcdefghij" * 10 + "\n"  # ~101 bytes per line w/ commas later
        # Simpler: write 5-col fixed-width lines
        line = ",".join(["xxxxxxxxxxxxxxxxxxxx"] * 5) + "\n"  # ~104 bytes
        # 100MB / 104 ≈ 1M rows
        for _ in range(1_000_000):
            f.write(line)
    assert big.stat().st_size > 90 * 1024 * 1024

    proc = psutil.Process()
    rss_before = proc.memory_info().rss
    preview, _r, _ = runner._preview_structured_streaming(
        big,
        mime="text/csv",
    )
    rss_after = proc.memory_info().rss
    rss_delta = rss_after - rss_before
    assert preview is not None
    assert len(preview["sample_first_5"]) == 5
    # Hard ceil per SRE P0-3/4
    assert rss_delta < 20 * 1024 * 1024, f"RSS delta {rss_delta / 1024 / 1024:.1f} MB exceeds 20MB cap"


def test_k_parquet_streaming_iter_batches(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    """test #7: parquet preview uses pf.iter_batches (NOT read_row_group)."""
    pq = pytest.importorskip("pyarrow.parquet")
    pa = pytest.importorskip("pyarrow")
    tbl = pa.table(
        {
            "a": list(range(10_000)),
            "b": [f"v{i}" for i in range(10_000)],
        }
    )
    pq_path = tmp_path / "small.parquet"
    pq.write_table(tbl, pq_path)

    # Patch iter_batches to verify it's called, NOT read_row_group
    real_pf = pq.ParquetFile
    seen_iter_batches = []

    class _SpyPF:
        def __init__(self, path):
            self._pf = real_pf(path)
            self.schema_arrow = self._pf.schema_arrow
            self.metadata = self._pf.metadata

        def iter_batches(self, *args, **kwargs):
            seen_iter_batches.append(kwargs)
            yield from self._pf.iter_batches(*args, **kwargs)

        def read_row_group(self, *_a, **_k):
            raise AssertionError(
                "K.2 invariant: parquet preview MUST iter_batches, not read_row_group (SRE P0-4)",
            )

    with patch.object(pq, "ParquetFile", _SpyPF):
        preview, _r, _ = runner._preview_structured_streaming(
            pq_path,
            mime="application/x-parquet",
        )
    assert preview is not None
    assert preview["columns"] == ["a", "b"]
    assert len(preview["sample_first_5"]) == 5
    assert preview["row_count"] == 10_000
    assert seen_iter_batches, "iter_batches was never called"


# ═════════════════════════════════════════════════════════
# K.3 — PII redaction
# ═════════════════════════════════════════════════════════


def test_k_pii_column_name_redacts_value(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    """test #8: column '手机号' → cell value masked even though pattern is
    a legit phone."""
    _write_csv(
        tmp_path / "phone.csv",
        ["公司", "手机号"],
        [["A 公司", "13888888888"]],
    )
    preview, _r, pii = runner._preview_structured_streaming(
        tmp_path / "phone.csv",
        mime="text/csv",
        redact_pii=True,
    )
    assert preview is not None
    # Column NAME preserved (LLM needs schema), VALUE masked.
    assert "手机号" in preview["columns"]
    assert preview["sample_first_5"][0]["手机号"] == "<redacted_col>"
    assert preview["sample_first_5"][0]["公司"] == "A 公司"
    assert any(r["redact_kind"] == "col" and "手机" in r["column_pattern"] for r in pii)


def test_k_pii_value_pattern_redacts(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    """test #9: column '客户ID' (no PII keyword) + value matching id_card
    pattern → val_match redact."""
    _write_csv(
        tmp_path / "id.csv",
        ["客户ID", "金额"],
        [["12345678901234567X", 100]],
    )
    preview, _r, pii = runner._preview_structured_streaming(
        tmp_path / "id.csv",
        mime="text/csv",
        redact_pii=True,
    )
    assert preview is not None
    # Column name "客户ID" does not match the column regex (no id_card / 身份证 token).
    # but value matches the cn_id_card pattern → val_match.
    assert preview["sample_first_5"][0]["客户ID"] == "<redacted_val>"
    assert any(r["redact_kind"] == "val" and r["column_pattern"] == "cn_id_card" for r in pii)


def test_k_pii_chinese_name_redacted(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    """test #10: column '客户姓名' → values redacted by col_match."""
    _write_csv(
        tmp_path / "name.csv",
        ["客户姓名", "amt"],
        [["张三", 50], ["李四", 70]],
    )
    preview, _r, pii = runner._preview_structured_streaming(
        tmp_path / "name.csv",
        mime="text/csv",
        redact_pii=True,
    )
    assert preview is not None
    assert preview["sample_first_5"][0]["客户姓名"] == "<redacted_col>"
    assert preview["sample_first_5"][1]["客户姓名"] == "<redacted_col>"
    assert sum(1 for r in pii if r["redact_kind"] == "col") == 2  # two rows × one masked column


# ═════════════════════════════════════════════════════════
# K.0 — flag + allowlist gating
# ═════════════════════════════════════════════════════════


def _resolve_via_runner(
    runner: ModuleType,
    csv_path: Path,
    workdir: Path,
    *,
    k_enabled: bool,
    redact_pii: bool = True,
) -> dict:
    """Helper: call _materialize_inputs directly (bypassing _fetch_artifact
    by stubbing it with a local file read)."""
    fake_data = csv_path.read_bytes()
    with patch.object(runner, "_fetch_artifact", return_value=fake_data):
        resolved, errs = runner._materialize_inputs(
            {"rows": {"artifact_id": "art_x", "filename": csv_path.name}},
            workdir,
            "http://gw",
            k_enabled=k_enabled,
            k_redact_pii=redact_pii,
        )
    assert not errs, f"unexpected errors: {errs}"
    return resolved


def test_k_flag_off_no_preview(runner: ModuleType, tmp_path: Path) -> None:
    """test #11: flag OFF → INPUTS entry has no `columns` key (legacy)."""
    csv = tmp_path / "x.csv"
    _write_csv(csv, ["a", "b"], [[1, 2]])
    workdir = tmp_path / "wd"
    workdir.mkdir()
    resolved = _resolve_via_runner(runner, csv, workdir, k_enabled=False)
    entry = resolved["rows"]
    # Legacy fields present
    assert entry["path"].endswith(".csv")
    assert entry["artifact_id"] == "art_x"
    # K.0: no schema injection when flag OFF
    assert "columns" not in entry
    assert "sample_first_5" not in entry
    assert "dtypes" not in entry
    # No sidecar metric file
    assert not (workdir / ".k_metrics.json").exists()


def test_k_flag_on_allowlist_miss_no_preview(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    """test #12: flag ON + ws not in allowlist (decided at tools.py layer).

    Runner itself has no allowlist awareness — gate is in tools.py
    `_k_should_enable`. Test the gate function directly.
    """
    from bowhead_skill.tools import _k_should_enable

    ctx = MagicMock(workspace_id="other-ws")
    with patch.dict(
        os.environ,
        {
            "BOWHEAD_PI_K_ENABLED": "1",
            "BOWHEAD_PI_K_ALLOWLIST_WORKSPACES": "datarc-demo,foo",
        },
    ):
        assert _k_should_enable(ctx) is False


def test_k_flag_on_allowlist_hit_preview(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    """test #13: flag ON + ws in allowlist → gate returns True."""
    from bowhead_skill.tools import _k_should_enable

    ctx = MagicMock(workspace_id="datarc-demo")
    with patch.dict(
        os.environ,
        {
            "BOWHEAD_PI_K_ENABLED": "1",
            "BOWHEAD_PI_K_ALLOWLIST_WORKSPACES": "datarc-demo,foo",
        },
    ):
        assert _k_should_enable(ctx) is True
    # Empty allowlist → all workspaces
    with patch.dict(
        os.environ,
        {
            "BOWHEAD_PI_K_ENABLED": "1",
            "BOWHEAD_PI_K_ALLOWLIST_WORKSPACES": "",
        },
    ):
        assert _k_should_enable(ctx) is True
    # Default ON (compute-agent-optimize batch 1); explicit "0" still turns it off.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BOWHEAD_PI_K_ENABLED", None)
        assert _k_should_enable(ctx) is True
    with patch.dict(os.environ, {"BOWHEAD_PI_K_ENABLED": "0"}):
        assert _k_should_enable(ctx) is False

    # And actually verify preview shows up with k_enabled=True
    csv = tmp_path / "y.csv"
    _write_csv(csv, ["a", "b"], [[1, 2], [3, 4]])
    workdir = tmp_path / "wd2"
    workdir.mkdir()
    resolved = _resolve_via_runner(runner, csv, workdir, k_enabled=True)
    entry = resolved["rows"]
    assert entry["columns"] == ["a", "b"]
    assert entry["sample_first_5"] == [
        {"a": "1", "b": "2"},
        {"a": "3", "b": "4"},
    ]


# ═════════════════════════════════════════════════════════
# K.4 — per-turn cache
# ═════════════════════════════════════════════════════════


def test_k_cache_per_turn_dedup(runner: ModuleType, tmp_path: Path) -> None:
    """test #14: same artifact_id 2× same materialize call → preview computed
    only once (per-call cache in _materialize_inputs).

    Note: the gateway-side per-turn cache (in tools.py) lives at a different
    layer. This test asserts the runner-level cache (same call dedup).
    """
    csv = tmp_path / "z.csv"
    _write_csv(csv, ["a"], [[1]])
    workdir = tmp_path / "wd3"
    workdir.mkdir()
    fake_data = csv.read_bytes()
    call_count = {"n": 0}
    real_fn = runner._preview_structured_streaming

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return real_fn(*args, **kwargs)

    with patch.object(runner, "_preview_structured_streaming", side_effect=_spy), patch.object(runner, "_fetch_artifact", return_value=fake_data):
        resolved, _ = runner._materialize_inputs(
            {
                "rows": {"artifact_id": "art_same", "filename": csv.name},
                "rows_alias": {
                    "artifact_id": "art_same",
                    "filename": csv.name,
                },
            },
            workdir,
            "http://gw",
            k_enabled=True,
            k_redact_pii=True,
        )
    assert call_count["n"] == 1, "preview should be cached across name aliases"
    assert resolved["rows"]["columns"] == ["a"]
    assert resolved["rows_alias"]["columns"] == ["a"]


# ═════════════════════════════════════════════════════════
# K.2 — unsupported mime fallthrough
# ═════════════════════════════════════════════════════════


def test_k_unsupported_mime_no_preview(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    """test #15: .bin → preview=None, reason='unsupported_mime'."""
    binp = tmp_path / "data.bin"
    binp.write_bytes(b"\x00\x01\x02\x03\x04")
    preview, reason, _ = runner._preview_structured_streaming(
        binp,
        mime="application/octet-stream",
    )
    assert preview is None
    assert reason == "unsupported_mime"


# ═════════════════════════════════════════════════════════
# K.7 — sidecar → Prom counter drain
# ═════════════════════════════════════════════════════════


def test_k_sidecar_metrics_drained(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    """test #16: runner writes sidecar JSONL with correct schema/PII records."""
    csv = tmp_path / "metrics.csv"
    _write_csv(csv, ["手机号", "金额"], [["13888888888", 100]])
    workdir = tmp_path / "wd_metric"
    workdir.mkdir()
    fake_data = csv.read_bytes()
    with patch.object(runner, "_fetch_artifact", return_value=fake_data):
        runner._materialize_inputs(
            {"rows": {"artifact_id": "aid_m", "filename": csv.name}},
            workdir,
            "http://gw",
            k_enabled=True,
            k_redact_pii=True,
        )
    sidecar = workdir / ".k_metrics.json"
    assert sidecar.is_file()
    lines = [json.loads(li) for li in sidecar.read_text(encoding="utf-8").splitlines() if li.strip()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["mime"] == "text/csv"
    assert rec["schema_present"] is True
    assert rec["bytes"] > 0
    # PII column matched → at least 1 pii record
    assert any(p["redact_kind"] == "col" for p in rec["pii"])


# ═════════════════════════════════════════════════════════
# K.5 — SKILL.md placeholder not hardcoded
# ═════════════════════════════════════════════════════════


def test_k_skill_md_placeholder_not_hardcoded_input_name() -> None:
    """test #17: SKILL.md should use `<input_name>` placeholder (or generic
    `name` like 'sales' shown only as ONE concrete example), not lock LLM
    into a fixed key name like 'rows'.

    Per Eng P0-2 fix: documentation in `## INPUTS 字段` and `## 写代码前看
    schema` sections must use `<input_name>` placeholder syntax.
    """
    md = (Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "SKILL.md").read_text(encoding="utf-8")
    # The new K section must reference <input_name> as placeholder
    assert "<input_name>" in md, "SKILL.md must use <input_name> placeholder per Eng P0-2 fix"
    # Find the K-specific section. Heuristic: section header phrase from
    # design doc.
    assert "INPUTS dict 字段" in md or "π K" in md or "Sprint π" in md, "SKILL.md missing π K INPUTS schema documentation"

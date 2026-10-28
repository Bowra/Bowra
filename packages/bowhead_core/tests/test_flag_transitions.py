"""flag 翻转时刻账本（上下文工程 sprint 缩小版方案 B）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from bowhead_core.flag_transitions import FILENAME, record_flag_transitions

_T0 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
_T1 = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


def _rows(d: Path) -> list[dict]:
    p = d / FILENAME
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_first_observation_on_is_recorded_with_version_and_time(tmp_path: Path) -> None:
    got = record_flag_transitions({"BOWHEAD_X": True}, version="1.2.26", data_dir=tmp_path, now=_T0)
    assert got == ["BOWHEAD_X"]
    assert _rows(tmp_path) == [{"flag": "BOWHEAD_X", "on": True, "version": "1.2.26", "at": "2026-09-05T10:00:00+00:00", "kind": "first_seen"}]


def test_first_observation_off_is_not_recorded(tmp_path: Path) -> None:
    assert record_flag_transitions({"BOWHEAD_X": False}, version="1.2.26", data_dir=tmp_path, now=_T0) == []
    assert not (tmp_path / FILENAME).exists()


def test_unchanged_state_on_restart_appends_nothing(tmp_path: Path) -> None:
    record_flag_transitions({"BOWHEAD_X": True}, version="1.2.26", data_dir=tmp_path, now=_T0)
    assert record_flag_transitions({"BOWHEAD_X": True}, version="1.2.27", data_dir=tmp_path, now=_T1) == []
    assert len(_rows(tmp_path)) == 1


def test_flip_off_then_on_again_records_both_transitions(tmp_path: Path) -> None:
    record_flag_transitions({"BOWHEAD_X": True}, version="1.2.26", data_dir=tmp_path, now=_T0)
    assert record_flag_transitions({"BOWHEAD_X": False}, version="1.2.26", data_dir=tmp_path, now=_T1) == ["BOWHEAD_X"]
    assert record_flag_transitions({"BOWHEAD_X": True}, version="1.2.27", data_dir=tmp_path, now=_T1) == ["BOWHEAD_X"]
    assert [(r["on"], r["kind"]) for r in _rows(tmp_path)] == [(True, "first_seen"), (False, "flip"), (True, "flip")]


def test_multiple_flags_are_independent(tmp_path: Path) -> None:
    got = record_flag_transitions({"A": True, "B": False, "C": True}, version="v", data_dir=tmp_path, now=_T0)
    assert got == ["A", "C"]
    assert [r["flag"] for r in _rows(tmp_path)] == ["A", "C"]


def test_corrupt_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    # Review fix: `[]` is valid JSON but not an object; the first version raised AttributeError, silently killing the ledger on every startup.
    (tmp_path / FILENAME).write_text('{"flag": "A", "on": true, "version": "v", "at": "t"}\nnot json\n[]\n42\n', encoding="utf-8")
    # A is already ON and unchanged → no record; corrupt lines do not affect reading.
    assert record_flag_transitions({"A": True}, version="v", data_dir=tmp_path, now=_T0) == []


def test_missing_data_dir_is_created(tmp_path: Path) -> None:
    d = tmp_path / "nested" / "sqlite"
    assert record_flag_transitions({"A": True}, version="v", data_dir=d, now=_T0) == ["A"]
    assert (d / FILENAME).exists()

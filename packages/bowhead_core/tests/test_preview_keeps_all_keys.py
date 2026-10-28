"""An oversized preview value must not hide other keys in the same object.

Long code and its inputs remain independently visible after compaction.
"""

from __future__ import annotations

import json

import pytest
from bowhead_core.subagent import _preview

pytestmark = pytest.mark.unit


def _sandbox_args(code_len: int) -> dict:
    return {
        "code": "x = 1  # " + "长" * code_len,
        "inputs": {"rows": {"artifact_id": "art_000000000001"}},
        "purpose": "analysis",
    }


def test_long_code_does_not_hide_inputs() -> None:
    """★ 命门：code 再长也要看得见 inputs —— 真机上正是它决定归因。"""
    out = _preview(_sandbox_args(4000))
    assert "inputs" in out and "art_000000000001" in out
    assert "purpose" in out, "排在最后的键也不许丢"
    assert len(out) <= 501


def test_declared_nothing_is_distinguishable_from_declared_and_lost() -> None:
    """★ 预览要能分开「没传 inputs」和「传了」——不然遥测里两者长得一样。"""
    without = _preview({"code": "y = 2  # " + "长" * 4000, "purpose": "analysis"})
    assert "inputs" not in without
    assert "inputs" in _preview(_sandbox_args(4000))


def test_short_payload_is_byte_identical_to_before() -> None:
    """★ 负对照：放得下的对象一个字节都不许变（收缩只发生在超限那条路上）。"""
    args = {"queries": ["营业额"], "limit": 50}
    assert _preview(args) == json.dumps(args, ensure_ascii=False, default=str)


def test_still_bounded_when_every_value_is_huge() -> None:
    """收缩到底仍放不下时退回截尾，与旧行为一致（长度上界不许破）。"""
    out = _preview({f"k{i}": "长" * 2000 for i in range(40)})
    assert len(out) <= 501


def test_non_dict_falls_back_to_tail_truncation() -> None:
    out = _preview(["长" * 2000])
    assert len(out) <= 501
    assert out.endswith("…")

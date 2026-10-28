"""BI 取数规模探针 —— **告警，不是上限**。

体积上限方案已被证否（docs/sprints/artifact-volume-2026-09-04/tech-design.md §4.3）：
全库 27 个 >10MB 的产物里 25 个真被载进沙箱、23 轮交付成功，砍了是让沙箱拿残缺 CSV
做 groupby 从而**静默算错**。所以这里只记录，绝不拒绝。

留探针的理由：那批 10–88MB 的行级明细在 09-02 之后自己停了，但归因不成立
（劝阻文案 09-02T16:29Z 才写出来、09-04 才上线，最后一次明细取数是 09-02T09:26Z）
⇒ 说不清是被修好了还是恰好没人这么问。复发时要当天看得见。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from bowhead_core.metrics import get_registry
from bowhead_core.tool_registry import (
    _BI_FETCH_OVERSIZED_BYTES,
    _bi_fetch_oversized_threshold,
    _record_bi_fetch_scale,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_OVERSIZED = "bowhead_bi_fetch_rows_oversized_total"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    get_registry().reset()
    yield
    get_registry().reset()


def _oversized_count() -> float:
    counter = get_registry()._counters.get(_OVERSIZED)
    return sum(counter._values.values()) if counter else 0.0


def test_normal_fetch_records_scale_without_alarming() -> None:
    """干净窗口里 bi_fetch_rows 的 p90 只有 31KB —— 正常取数绝不该告警。"""
    _record_bi_fetch_scale(byte_size=31_000, row_count=380, query_text="2026年各品牌营业额")
    assert _oversized_count() == 0.0
    assert get_registry()._histograms.get("bowhead_bi_fetch_rows_bytes") is not None
    assert get_registry()._histograms.get("bowhead_bi_fetch_rows_row_count") is not None


def test_oversized_fetch_alarms_with_actionable_context(caplog: pytest.LogCaptureFixture) -> None:
    """复发时要当天看得见，且日志里得有「哪条 query 干的」。"""
    with caplog.at_level(logging.WARNING, logger="bowhead_core.tool_registry"):
        _record_bi_fetch_scale(
            byte_size=88 * 1024 * 1024,
            row_count=1_200_000,
            query_text="2026年3月城市营业额明细",
        )
    assert _oversized_count() == 1.0
    assert any("bi_fetch_rows.oversized" in r.message for r in caplog.records)
    assert any("明细" in str(r.args) for r in caplog.records), "日志必须带上闯祸的那条 query"


def test_threshold_is_env_tunable_without_restart() -> None:
    """运维要能不重启就调；坏值一律退默认，绝不因为 env 写错就把取数打挂。"""
    import os

    assert _bi_fetch_oversized_threshold() == _BI_FETCH_OVERSIZED_BYTES
    os.environ["BOWHEAD_BI_FETCH_OVERSIZED_BYTES"] = "1024"
    try:
        assert _bi_fetch_oversized_threshold() == 1024
        _record_bi_fetch_scale(byte_size=2048, row_count=5, query_text="x")
        assert _oversized_count() == 1.0
        for bad in ("", "abc", "0", "-1"):
            os.environ["BOWHEAD_BI_FETCH_OVERSIZED_BYTES"] = bad
            assert _bi_fetch_oversized_threshold() == _BI_FETCH_OVERSIZED_BYTES, f"坏值 {bad!r} 应退默认"
    finally:
        os.environ.pop("BOWHEAD_BI_FETCH_OVERSIZED_BYTES", None)


def test_probe_never_breaks_a_successful_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """埋点挂了也只是少一条观测，绝不能让一次已经成功的取数失败。"""
    import bowhead_core.metrics as _m

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("registry down")

    monkeypatch.setattr(_m, "observe", _boom)
    _record_bi_fetch_scale(byte_size=1, row_count=1, query_text="x")  # No exception means pass.


def test_both_are_actually_wired_into_fetch_rows() -> None:
    """★ 防「写好了没接线」。

    上面那些测试直接调 `_record_bi_fetch_scale` / `put_bytes`，即使 `_fetch_rows` 里
    一行都没改也照样全绿。本 sprint 的调研在这个子系统里数出**六套**「本该管住量」的机制
    全是死的，其中 `artifact_stdout` 表和 `turn_attachments` 表都是建好了、有测试、
    但生产零写入方 —— 判存活的判据是「谁写它」，不是「有没有测试」。

    判据用 AST：真跑一次 `_fetch_rows` 要拉起 datarc client / artifact store / ctx 一整套。
    """
    import ast
    import inspect

    from bowhead_core import tool_registry

    tree = ast.parse(inspect.getsource(tool_registry))
    fetch = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "_fetch_rows")

    put_bytes_calls = [
        node for node in ast.walk(fetch) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "put_bytes"
    ]
    assert put_bytes_calls, "_fetch_rows 里找不到 put_bytes —— artifact 落库口挪走了？"
    assert any(kw.arg == "row_count" for call in put_bytes_calls for kw in call.keywords), (
        "_fetch_rows 的 put_bytes 没传 row_count —— 行数只在这一刻算得出，漏了就永远拿不回来"
    )

    called = {node.func.id for node in ast.walk(fetch) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "_record_bi_fetch_scale" in called, "规模探针没接进 _fetch_rows —— 复发时不会有任何人知道"

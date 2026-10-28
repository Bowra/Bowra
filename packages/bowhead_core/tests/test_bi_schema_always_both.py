"""bi_schema always fetches both measures and dimensions.

Legacy kind arguments must not fabricate an empty list and misrepresent source
capabilities in step previews.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from bowhead_bi_bridge.types import DimensionMeta, MeasureMeta
from bowhead_core.tool_registry import build_default_registry

_SRC = Path(__file__).parent.parent / "src" / "bowhead_core"
_REPO = Path(__file__).parent.parent.parent.parent


def _dim(i: int, name: str, used: bool = True) -> DimensionMeta:
    return DimensionMeta(id=i, names=[name], is_keyword=True, used=used)


def _measure(i: int, name: str) -> MeasureMeta:
    return MeasureMeta(id=i, names=[name])


class _Datarc:
    """可控的 datarc：记录每个 endpoint 被调了几次，并可让某一侧先返空。"""

    def __init__(self, *, measures: list[Any], dims: list[Any], empty_measures_times: int = 0, empty_dims_times: int = 0) -> None:
        self._m, self._d = measures, dims
        self._em, self._ed = empty_measures_times, empty_dims_times
        self.m_calls = self.d_calls = 0

    async def list_measures(self, ws: str) -> list[Any]:
        self.m_calls += 1
        if self._em > 0:
            self._em -= 1
            return []
        return self._m

    async def list_dimensions(self, ws: str) -> list[Any]:
        self.d_calls += 1
        if self._ed > 0:
            self._ed -= 1
            return []
        return self._d


class _Ctx:
    """Duck-typed ToolExecutionContext —— `_schema` 只用 .datarc + resolve_datarc_project()."""

    def __init__(self, datarc: Any) -> None:
        self.datarc = datarc
        self.member_uid = "m_test"

    def resolve_datarc_project(self) -> str:
        return "355"


async def _call(args: dict[str, Any], datarc: Any) -> Any:
    return await build_default_registry().get("bi_schema").handler(args, _Ctx(datarc))


# ── Always both ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_no_args_returns_both() -> None:
    d = _Datarc(measures=[_measure(1, "销售金额")], dims=[_dim(199, "中文品牌")])
    out = await _call({}, d)
    assert len(out["measures"]) == 1
    assert len(out["dimensions"]) == 1


@pytest.mark.anyio
async def test_legacy_kind_is_ignored_not_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 核心回归：`kind="dimensions"` 不再让 measures 变成空列表。

    旧行为：该分支 `measures = []`（构造出来的空）⇒ 用户面「0 测度」。
    新行为：参数被忽略，两侧都真查。
    """
    # ★ The distinguishing check is whether a retry occurred, not whether data was eventually returned.
    # If the kind branch returns: measures starts empty → symmetric retry fills it → result-only assertions,
    # even call-count-only assertions, **still pass** (both observed). Retrying necessarily sleeps,
    # but healthy datarc must never sleep even once.
    slept: list[float] = []
    import asyncio as _a

    _real_sleep = _a.sleep

    async def _spy(sec: float, *a: Any, **k: Any) -> Any:
        slept.append(sec)
        return await _real_sleep(0)

    monkeypatch.setattr(_a, "sleep", _spy)

    d = _Datarc(measures=[_measure(1, "销售金额")], dims=[_dim(199, "中文品牌")])
    out = await _call({"kind": "dimensions"}, d)
    assert out.get("error") is None, "残留传参不该报错"
    assert len(out["measures"]) == 1, "measures 仍被构造成空 ⇒ 「0 测度」会回来"
    assert not slept, f"measures 是靠重试兜底才拿到的（sleep {slept}）——说明首次没并发拉它"


@pytest.mark.anyio
async def test_schema_no_longer_advertises_kind() -> None:
    """工具 schema 里不能再有 `kind`——留着就是在教模型传一个已忽略的参数。"""
    props = build_default_registry().get("bi_schema").schema.input_schema.get("properties") or {}
    assert "kind" not in props


# ── Symmetric retry for measures (a live risk only after always-both behavior) ───


@pytest.mark.anyio
async def test_measures_empty_but_dims_ok_retries_and_warns() -> None:
    """此前只有 dims 侧有重试+告警，measures 侧什么都没有。

    判据与 dims 侧同源：**另一侧正常**才算非对称指纹（两边都空多半是空 workspace）。
    """
    d = _Datarc(measures=[_measure(1, "销售金额")], dims=[_dim(199, "中文品牌")], empty_measures_times=99)
    out = await _call({}, d)
    assert d.m_calls >= 3, f"没重试，只调了 {d.m_calls} 次"
    warns = " ".join(out.get("_schema_warnings") or [])
    assert "指标列表" in warns
    assert "没有指标" in warns, "必须明确告诉模型别断言「本 workspace 没有指标」"


@pytest.mark.anyio
async def test_measures_recovers_on_retry_no_warning() -> None:
    """正对照：第一次空、重试拿到了 ⇒ 不该报警（否则每次抖动都吓模型一跳）。"""
    d = _Datarc(measures=[_measure(1, "销售金额")], dims=[_dim(199, "中文品牌")], empty_measures_times=1)
    out = await _call({}, d)
    assert len(out["measures"]) == 1
    assert not (out.get("_schema_warnings") or [])


@pytest.mark.anyio
async def test_both_empty_does_not_warn() -> None:
    """两边都空 = 多半是空 workspace，不是抖动 ⇒ 不报「疑似抖动」。"""
    d = _Datarc(measures=[], dims=[], empty_measures_times=99, empty_dims_times=99)
    out = await _call({}, d)
    assert not (out.get("_schema_warnings") or [])


# ── Summary must no longer falsely report 「0 测度」 ──────────────────


def test_summary_omits_empty_segment() -> None:
    from bowhead_core.subagent import _prettify_output_preview

    s = _prettify_output_preview("bi_schema", {"measures": [], "dimensions": [{"id": 1}] * 101}, None, ui_language="zh-CN")
    assert "0 测度" not in s, "空列表仍被渲成「0 测度」——用户报的就是这个"
    assert "101 维度" in s


def test_summary_keeps_both_when_both_present() -> None:
    """正对照：两段都有内容时照常都打（别为了消灭 0 而把段丢了）。"""
    from bowhead_core.subagent import _prettify_output_preview

    s = _prettify_output_preview("bi_schema", {"measures": [{"id": 1}] * 27, "dimensions": [{"id": 1}] * 101}, None, ui_language="zh-CN")
    assert "27 测度" in s and "101 维度" in s


# ── Drift-prevention guard ─────────────────────────────────────────


def test_no_prompt_still_teaches_kind() -> None:
    """★ 全仓不得再有 `bi_schema(kind=`。

    实施时发现设计写的「四处提示语」是错的：`bowhead_skill/catalog/*/SKILL.md`
    里还有 **48 处**在教模型传 `kind`，而那些是运行时 `skill_read` 真读的。
    改一处等于没改（feedback_one_concept_many_implementations），所以留这道闸。
    """
    hits: list[str] = []
    for pat in ("*.py", "*.md", "*.yaml", "*.json"):
        for f in (_REPO / "packages").rglob(pat):
            if "/tests/" in str(f) or f.name == Path(__file__).name:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if re.search(r"bi_schema\(kind=", text):
                hits.append(str(f.relative_to(_REPO)))
    assert not hits, "这些地方还在教模型给 bi_schema 传 kind：\n" + "\n".join(sorted(set(hits)))

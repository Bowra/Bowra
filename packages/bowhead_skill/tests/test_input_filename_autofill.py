"""inputs 只需给 artifact_id —— 文件名由宿主侧按 id 补齐.

2026-08-18 现场：我们的 tool description 明确教模型把
`"filename": "2025年1-7月onc产品线销售数量中文省份_21b5fc4.csv"` 原样打进 JSON。实测这正是
流式 tool-args 写崩的位置——`skill_invoke` 报 `Expecting ',' delimiter (offset=857)`，附近原文
是同一串中文长文件名被重复吐了一遍还带转义，整次调用因 JSON 不合法作废（同一天
`subagent_return` 也有一次 char 2132 的 JSON 解析失败，同族）。

后端自己就知道 artifact 叫什么。让模型逐字复述只是加长 payload、多一个出错面。
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_skill.tools import _fill_input_filenames


class _Store:
    def __init__(self, mapping: dict[str, str], *, boom: bool = False) -> None:
        self._m = mapping
        self._boom = boom
        self.calls: list[str] = []

    async def get(self, aid: str, **_kw):
        self.calls.append(aid)
        if self._boom:
            raise RuntimeError("store down")
        fn = self._m.get(aid)
        return SimpleNamespace(filename=fn) if fn else None


async def test_filename_is_filled_from_the_artifact_store() -> None:
    data = {"inputs": {"sales": {"artifact_id": "art_a"}, "growth": {"artifact_id": "art_b"}}}
    store = _Store({"art_a": "2026年1-7月销售金额_e094413.csv", "art_b": "同比_67dfbe2.csv"})
    await _fill_input_filenames(data, SimpleNamespace(artifact_store=store))
    assert data["inputs"]["sales"]["filename"] == "2026年1-7月销售金额_e094413.csv"
    assert data["inputs"]["growth"]["filename"] == "同比_67dfbe2.csv"


async def test_explicit_filename_is_never_overwritten() -> None:
    """★ 负对照：调用方显式给了名字就是有意的，不许改。"""
    data = {"inputs": {"sales": {"artifact_id": "art_a", "filename": "我自己起的.csv"}}}
    store = _Store({"art_a": "后端的真名.csv"})
    await _fill_input_filenames(data, SimpleNamespace(artifact_store=store))
    assert data["inputs"]["sales"]["filename"] == "我自己起的.csv"
    assert store.calls == []  # Do not even perform a lookup.


async def test_lookup_failure_degrades_instead_of_breaking_the_call() -> None:
    """补名失败不能让整次 sandbox 调用挂掉——runner 有既有降级路径。"""
    data = {"inputs": {"sales": {"artifact_id": "art_a"}}}
    await _fill_input_filenames(data, SimpleNamespace(artifact_store=_Store({}, boom=True)))
    assert "filename" not in data["inputs"]["sales"]

    data2 = {"inputs": {"sales": {"artifact_id": "art_missing"}}}
    await _fill_input_filenames(data2, SimpleNamespace(artifact_store=_Store({})))
    assert "filename" not in data2["inputs"]["sales"]


async def test_no_store_or_no_inputs_is_a_noop() -> None:
    data = {"inputs": {"sales": {"artifact_id": "art_a"}}}
    await _fill_input_filenames(data, SimpleNamespace())  # No artifact_store.
    assert data == {"inputs": {"sales": {"artifact_id": "art_a"}}}
    d2: dict = {"code": "print(1)"}
    await _fill_input_filenames(d2, SimpleNamespace(artifact_store=_Store({"art_a": "x.csv"})))
    assert d2 == {"code": "print(1)"}


async def test_autofilled_name_still_yields_the_right_extension() -> None:
    """端到端意义：补名的唯一用途是给 inputs/<key><ext> 提供扩展名。"""
    import importlib.util
    import sys
    from pathlib import Path

    rp = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"
    spec = importlib.util.spec_from_file_location("_ext_runner_under_test", rp)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ext_runner_under_test"] = mod
    spec.loader.exec_module(mod)

    data = {"inputs": {"sales": {"artifact_id": "art_a"}}}
    await _fill_input_filenames(data, SimpleNamespace(artifact_store=_Store({"art_a": "很长的中文名_e094413.xlsx"})))
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        t = mod._input_target(Path(td), "sales", data["inputs"]["sales"]["filename"], set())
        assert t.name == "sales.xlsx"  # Preserve the extension, without the long name in the path.

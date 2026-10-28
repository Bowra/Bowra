"""输入落盘约定：`inputs/<key><ext>` —— 短、可预测、按 key 命名.

2026-08-18 某客户实测：旧形态是 `/workdir/sales_onc产品线2026年1-7月销售金额大区经理辖区
_e094413.csv`（key + artifact 原名拼接）。模型记不住也抄不对，于是自己编
`INPUTS/sales/xxx.csv`、`/data/inputs/xxx.csv`、光秃秃一个文件名——它本来就期待一个目录
约定，只是我们没给。现在给：`inputs/sales.csv`，名字就是它自己声明的 key。

`INPUTS[key]['path']` 仍是唯一权威取法（契约形状不变），这里锁的是「路径别再长成没人抄得
对的样子」。
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_layout_runner_under_test", _RUNNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_layout_runner_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_target_is_inputs_dir_named_by_key(tmp_path) -> None:
    r = _runner()
    taken: set[str] = set()
    t = r._input_target(tmp_path, "sales", "onc产品线2026年1-7月销售金额大区经理辖区_e094413.csv", taken)
    assert t.parent.name == "inputs"
    assert t.name == "sales.csv"  # ← No longer includes the artifact's original name.
    assert t.parent.is_dir()


def test_extension_follows_the_source_file(tmp_path) -> None:
    """扩展名要留 —— 模型靠它决定 read_csv 还是 read_excel。"""
    r = _runner()
    taken: set[str] = set()
    assert r._input_target(tmp_path, "tpl", "月报模板.xlsx", taken).name == "tpl.xlsx"
    assert r._input_target(tmp_path, "raw", "no_ext_file", taken).name == "raw"


def test_colliding_keys_never_overwrite_each_other(tmp_path) -> None:
    """★ 两个 key 清洗后撞名时必须错开：覆盖 = 一份数据被另一份悄悄顶掉，
    而且是**静默**的（读得到文件、内容是别人的）。"""
    r = _runner()
    taken: set[str] = set()
    a = r._input_target(tmp_path, "sales/2026", "a.csv", taken)
    b = r._input_target(tmp_path, "sales_2026", "b.csv", taken)
    assert a.name != b.name, (a.name, b.name)


def test_manifest_path_points_at_the_new_layout(tmp_path, monkeypatch) -> None:
    """端到端：INPUTS[key]['path'] 必须指向 inputs/<key>.csv 且文件真的在那。"""
    r = _runner()
    monkeypatch.setattr(r, "_fetch_artifact", lambda *_a, **_kw: b"col\n1\n")
    resolved, errors = r._materialize_inputs(
        {"sales": {"artifact_id": "art_x", "filename": "很长的原始名字_abc123.csv"}},
        tmp_path,
        "http://gw",
    )
    assert errors == []
    assert resolved["sales"]["path"] == "/workdir/inputs/sales.csv"
    assert (tmp_path / "inputs" / "sales.csv").read_bytes() == b"col\n1\n"
    assert resolved["sales"]["sha256"] == hashlib.sha256(b"col\n1\n").hexdigest()
    assert resolved["sales"]["size"] == len(b"col\n1\n")

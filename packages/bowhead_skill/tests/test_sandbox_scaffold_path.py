"""python_sandbox 注入的 scaffolding 必须**两个后端都能读到 input.json**。

2026-08-06 本地 dogfood 定位：`_SCAFFOLD` 写死了 `/workdir/input.json`（容器内路径）。
docker 后端下 workdir 挂在 `/workdir` 所以成立；**subprocess 后端在宿主机跑**，cwd 是
`/private/var/folders/.../bow-skill-python_sandbox-xxx`，`/workdir` 根本不存在 →
每一次调用都 `FileNotFoundError` 挂在第 3 行，用户代码一行没跑。

后果：`BOWHEAD_SANDBOX_BACKEND=subprocess`（dev/CI 唯一不依赖 docker 的路径）对**任何**
注入本 scaffolding 的调用全失败——本地 dogfood 计算类问题完全跑不通，而这恰恰是验证
反造假守卫最需要的场景。

改成按 `__file__` 定位后两个后端都成立（docker 下 user.py 就在 /workdir/ 里，结果不变）。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_RUNNER = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("_bh_python_sandbox_runner_under_test", _RUNNER)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_runner_source_is_where_the_contract_says() -> None:
    """路径写死在测试里，脚本搬家时这条先红而不是让加载静默失败。"""
    assert _RUNNER.is_file(), f"python_sandbox runner not found at {_RUNNER}"


def test_scaffold_does_not_hardcode_container_workdir() -> None:
    """回归钉子：scaffolding 里不得再出现写死的 `/workdir/` 绝对路径。"""
    scaffold = _load_runner()._SCAFFOLD
    assert "/workdir/input.json" not in scaffold


def test_scaffold_reads_input_next_to_the_script(tmp_path: Path) -> None:
    """把 scaffolding + input.json 落到任意目录执行 —— 必须能读出 INPUTS。

    直接跑真 scaffolding（不是复述它的逻辑），所以它一旦改回写死路径这条就红。
    tmp_path 刻意**不是** /workdir，复刻 subprocess 后端的真实现场。
    """
    scaffold = _load_runner()._SCAFFOLD
    (tmp_path / "input.json").write_text(
        json.dumps({"inputs": {"rows": [1240, 980, 1410]}}),
        encoding="utf-8",
    )
    (tmp_path / "user.py").write_text(
        scaffold + "print(sum(INPUTS['rows']))\n",
        encoding="utf-8",
    )

    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(tmp_path / "user.py")],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "3630"


def test_scaffold_still_works_when_cwd_is_elsewhere(tmp_path: Path) -> None:
    """cwd 与脚本不同目录时也要成立 —— 判据是脚本位置，不是当前目录。"""
    scaffold = _load_runner()._SCAFFOLD
    work = tmp_path / "wd"
    work.mkdir()
    (work / "input.json").write_text(json.dumps({"inputs": {"n": 7}}), encoding="utf-8")
    (work / "user.py").write_text(scaffold + "print(INPUTS['n'])\n", encoding="utf-8")

    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(work / "user.py")],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),  # Deliberately outside the script's directory.
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "7"

"""系统 caveat 只能从一个入口写进 payload —— review 第 7 条的强制层。

背景（canvas-elements tech-design-document-minimalism §2.3）：渲染侧按「有没有机器标记」
区分两类 caveat：作者自己写的（交回正文，系统不渲）和系统生成的（照渲）。
判据是**字符串前缀**，所以任何一处代码忘了盖标记，那条诚实性信息就静默掉进丢弃桶。

第一版实现就漏了四处（peer 失败披露 / 计算台账对不齐 / 计划回退说明 / 组稿备注），
单测和类型都拦不住——因为「作者的 = 系统的补集」这个不变量当时没有任何一处在强制执行。
这个文件就是那个强制层。

★判据要精确到「**代码在新写一条 caveat 文案**」：参数是字符串字面量或 f-string 才算。
搬运已有条目（跨腿合并作者的局限、把列表回写原对象修同源）**不是**新写，不该误伤——
第一版用正则按语句形状扫，就把这两种正常写法一起报了。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from bowhead_core.analysis_payload import (
    PLAN_GAP_CAVEAT_PREFIX,
    append_system_caveat,
    author_caveats,
    plan_gap_caveats,
    strip_caveat_marker,
    system_caveats,
)

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[1] / "src" / "bowhead_core"
# Definitions, classification functions, and the single write entry live here.
_ALLOWED_FILES = {"analysis_payload.py"}


def _py_files() -> list[Path]:
    return [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts]


def _targets_caveats(node: ast.AST) -> bool:
    """`X["caveats"].append(...)` 或 `X.setdefault("caveats", []).append(...)` 的接收端。"""
    if isinstance(node, ast.Subscript):
        sl = node.slice
        return isinstance(sl, ast.Constant) and sl.value == "caveats"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault":
        return bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value == "caveats"
    return False


def _authors_new_text(call: ast.Call) -> bool:
    """append 的参数是**新写的文案**（字面量 / f-string / 字面量拼接），而不是搬运已有条目。"""
    if len(call.args) != 1:
        return False
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return True
    if isinstance(arg, ast.JoinedStr):  # f-string
        return True
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
        return any(isinstance(side, ast.Constant | ast.JoinedStr) for side in (arg.left, arg.right))
    return False


def _violations(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if '"caveats"' not in text:
        return []
    out: list[str] = []
    for node in ast.walk(ast.parse(text, filename=str(path))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append"):
            continue
        if not _targets_caveats(node.func.value) or not _authors_new_text(node):
            continue
        try:
            where = str(path.relative_to(_SRC))
        except ValueError:  # Positive-control temporary files are outside src.
            where = path.name
        out.append(f"{where}:{node.lineno}")
    return out


def test_no_bare_caveat_append_outside_the_single_writer() -> None:
    """★扫源码：不许绕过 `append_system_caveat` 新写一条 caveat 文案。

    绕过去的后果不是报错，是**静默丢失一条诚实性信息**（报告看起来完整、其实缺了一块），
    这类问题没有任何运行时信号，只能在这里钉住。
    新增系统 caveat 时：调 `append_system_caveat(payload, "……")`，不要自己 append。
    """
    offenders = [v for p in _py_files() if p.name not in _ALLOWED_FILES for v in _violations(p)]
    assert not offenders, "这些地方绕过了 append_system_caveat，系统 caveat 会被当成作者局限静默丢掉：\n" + "\n".join(offenders)


def test_the_guard_can_actually_see_a_violation(tmp_path: Path) -> None:
    """★正对照：上面那条断言「没发生」，必须证明它**看得见**违规、且**不误伤**搬运。
    否则判据写错、扫不到任何东西，它也会绿。"""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def f(payload, d, label):\n"
        '    payload["caveats"].append("这步失败了")\n'
        '    payload.setdefault("caveats", []).append(f"「{label}」失败")\n'
        '    d["caveats"].append("前缀" + label)\n',
        encoding="utf-8",
    )
    assert len(_violations(bad)) == 3, _violations(bad)

    ok = tmp_path / "ok.py"
    ok.write_text(
        "def f(payload, merged, cav, existing):\n"
        '    append_system_caveat(payload, "这步失败了")\n'
        '    merged["caveats"].append(cav)            # 搬运各腿作者写的\n'
        '    payload["caveats"] = list(existing)      # 回写原对象修同源\n'
        '    xs = payload.get("caveats") or []\n',
        encoding="utf-8",
    )
    assert _violations(ok) == [], _violations(ok)


def test_append_system_caveat_marks_and_dedupes() -> None:
    payload: dict = {}
    append_system_caveat(payload, "「compute」这步执行失败")
    append_system_caveat(payload, "「compute」这步执行失败")  # Idempotent.
    append_system_caveat(payload, PLAN_GAP_CAVEAT_PREFIX + "已经带标记的")  # Do not mark twice.
    append_system_caveat(payload, "   ")  # Discard whitespace.
    assert payload["caveats"] == [
        PLAN_GAP_CAVEAT_PREFIX + "「compute」这步执行失败",
        PLAN_GAP_CAVEAT_PREFIX + "已经带标记的",
    ]


def test_three_way_split_and_marker_is_never_user_visible() -> None:
    """三类各归各位；机器标记只给分类用，读出来给人看的那一份不带它。"""
    from bowhead_core.analysis_payload import FABRICATION_CAVEAT_PREFIX

    payload = {"caveats": ["样本量偏小"]}
    append_system_caveat(payload, "「compute」这步执行失败")
    payload["caveats"].insert(0, FABRICATION_CAVEAT_PREFIX + "KPI 3")

    assert author_caveats(payload) == ["样本量偏小"]
    assert len(system_caveats(payload)) == 2
    assert plan_gap_caveats(payload) == [PLAN_GAP_CAVEAT_PREFIX + "「compute」这步执行失败"]
    assert strip_caveat_marker(plan_gap_caveats(payload)[0]) == "「compute」这步执行失败"
    # The guard's accusation prefix **is the explanation itself** (followed by a numeric list), so do not strip it.
    assert strip_caveat_marker(FABRICATION_CAVEAT_PREFIX + "KPI 3").startswith(FABRICATION_CAVEAT_PREFIX)

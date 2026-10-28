"""credit-billing gap 24 —— retry 端点接真 re-dispatch 时必须过 entitlement gate 的守卫。

`routes/turns.py` 的 `retry_dispatch` / `retry_subtask` / `retry_step` 目前都是 **stub**：只校验 +
发 ack 事件 + 返回占位 id，**从不真重跑**（FE 的 retry 按钮已因此摘除，当前无调用方，端点仅保留
API 兼容）。它们的 docstring 都自警「接真 re-dispatch 时必须先过 `gate_turn_http`，否则成为绕过
授权与额度的新 turn 入口」（LICENSING §3.6）。

问题是这条自警只是注释——真有人接线时没有任何东西会阻止他忘掉。本文件把它变成会红的闸。

⚠ **本守卫必须剥掉 docstring 与注释再扫**：初版没剥，结果 docstring 里那句「必须过
gate_turn_http」自己就命中了 gate 标志，于是即便注入真的 `run_turn(` 调用守卫也照绿 —— 一个
纯粹的假守卫。下面 `test_self_check_*` 两条把这个失败模式本身钉住了。

这是**预防性**守卫，不是回归测试：当前（仍是 stub）状态下它应当通过。
"""

from __future__ import annotations

import re
from pathlib import Path

_TURNS_PY = Path(__file__).resolve().parents[3] / "packages/bowhead_gateway/src/bowhead_gateway/routes/turns.py"

#: Actual turn-dispatch markers; any match means endpoint is no longer a stub.
_DISPATCH_MARKERS = ("run_turn(", "spawn_turn(", "create_task(")
#: Authorization preflight markers.
_GATE_MARKERS = ("gate_turn_http", "assert_can_run_turn")
#: All three retry endpoints guarded (first version omitted retry_dispatch, exactly where the warning comment was).
_GUARDED = ("retry_dispatch", "retry_subtask", "retry_step")


def strip_prose(body: str) -> str:
    """去掉 docstring / 字符串字面量 / `#` 注释，只留可执行代码。

    不这么做，「必须过 gate_turn_http」这类**说明文字**会被当成真的接了 gate。
    """
    without_docstrings = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "", body)
    without_strings = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', '""', without_docstrings)
    return re.sub(r"#[^\n]*", "", without_strings)


def function_body(source: str, name: str) -> str:
    """截取 `async def <name>(` 到下一个顶层 `def` / `@router` 之间的正文。"""
    start = re.search(rf"^async def {re.escape(name)}\(", source, re.MULTILINE)
    if start is None:
        start = re.search(rf"^def {re.escape(name)}\(", source, re.MULTILINE)
    assert start is not None, f"没找到 {name} —— 端点被改名或删除, 本守卫需要同步更新"
    rest = source[start.end() :]
    nxt = re.search(r"^(@router\.|async def |def )", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _assert_gated_if_dispatching(name: str) -> None:
    code = strip_prose(function_body(_TURNS_PY.read_text(encoding="utf-8"), name))
    dispatching = [m for m in _DISPATCH_MARKERS if m in code]
    if not dispatching:
        return  # Still stub, as currently expected.
    assert any(m in code for m in _GATE_MARKERS), (
        f"{name} 现在会真派 turn（命中 {dispatching}）但没有 entitlement 预检。\n"
        f"必须先过 bowhead_gateway._entitlement_gate.gate_turn_http，否则它成为一个绕过授权"
        f"（以及 chat.py 那段余额 preflight）的新 turn 入口 —— 见该函数 docstring 的 LICENSING §3.6 自警。"
    )


def test_retry_endpoints_gated_if_they_dispatch() -> None:
    for name in _GUARDED:
        _assert_gated_if_dispatching(name)


def test_guard_still_finds_its_targets() -> None:
    """守卫自检：端点改名/删除时显式失败，而不是静默变永绿。"""
    source = _TURNS_PY.read_text(encoding="utf-8")
    for name in _GUARDED:
        assert function_body(source, name).strip(), name


def test_self_check_detects_ungated_dispatch() -> None:
    """★ 反向自检：真派 turn 且无 gate 的代码必须被判为未 gated。"""
    fake = "async def retry_step(x):\n    result = await orchestrator.run_turn(req)\n    return result\n"
    code = strip_prose(function_body(fake, "retry_step"))
    assert any(m in code for m in _DISPATCH_MARKERS), "dispatch 标志匹配失效, 守卫已无效"
    assert not any(m in code for m in _GATE_MARKERS)


def test_self_check_docstring_mention_does_not_count_as_gated() -> None:
    """★★ 初版就是死在这里：docstring 里提到 gate_turn_http 不等于真的接了 gate。"""
    fake = (
        "async def retry_step(x):\n"
        '    """接真 re-dispatch 时必须过 gate_turn_http, 否则成为免费 turn 入口。"""\n'
        "    result = await orchestrator.run_turn(req)\n"
        "    return result\n"
    )
    code = strip_prose(function_body(fake, "retry_step"))
    assert any(m in code for m in _DISPATCH_MARKERS)
    assert not any(m in code for m in _GATE_MARKERS), "说明文字被当成了真 gate —— 守卫又变假了"


def test_self_check_real_gate_call_counts() -> None:
    """反向的反向：真的调用了 gate 就该判为已 gated（别把守卫做成永远失败）。"""
    fake = "async def retry_step(x):\n    await gate_turn_http(request, team_id)\n    result = await orchestrator.run_turn(req)\n    return result\n"
    code = strip_prose(function_body(fake, "retry_step"))
    assert any(m in code for m in _DISPATCH_MARKERS)
    assert any(m in code for m in _GATE_MARKERS)

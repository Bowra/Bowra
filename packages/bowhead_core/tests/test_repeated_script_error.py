"""A script that fails again with an error this run has already seen gets one sentence that says so.

Grounded in the 116 template fill of 2026-11-16 (the regression case, Turn the regression case): 11 of 15
script failures were the same IndexError from python-pptx's table.cell, spread over 2.5 minutes and
interleaved with successful debugging calls, so the failures-in-a-row gate never applied. Nothing told
the model the error was the same. The sentence states the fact and refuses nothing.
"""

from __future__ import annotations

import pytest
from bowhead_core.subagent import _with_repeated_failure, script_error_signature
from bowhead_core.tool_registry import ToolInvocation

pytestmark = [pytest.mark.unit]

_TABLE_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "/workdir/user.py", line {line}, in <module>\n'
    "    cell_set(t.cell(i, c), v)\n"
    '  File "/usr/local/lib/python3.12/site-packages/pptx/table.py", line 40, in cell\n'
    "    return _Cell(self._tbl.tc(row_idx, col_idx), self)\n"
    "IndexError: list index out of range\n"
    "\n[runner] declared deliverable outputs were not produced and persisted: deck.pptx"
)


def _failed(stderr: str, tool: str = "python_exec") -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool,
        arguments={},
        result={"ok": False, "stderr": "", "result_json": {"ok": False, "exit_code": 1, "stderr": stderr}, "error": "exit_code=1"},
    )


def test_the_signature_is_the_exception_and_the_library_frame_not_the_script_line():
    first = script_error_signature(_failed(_TABLE_ERROR.format(line=178)).result)
    assert first == ("IndexError: list index out of range", "pptx/table.py:40")
    # The script changed between attempts; the error did not.
    assert script_error_signature(_failed(_TABLE_ERROR.format(line=146)).result) == first
    own = "Traceback (most recent call last):\n  File \"/workdir/user.py\", line 9, in <module>\nKeyError: '月份'\n"
    assert script_error_signature(_failed(own).result) == ("KeyError: '月份'", "")


def test_the_second_identical_error_states_how_many_came_before_and_others_say_nothing():
    seen: dict = {}
    first = _with_repeated_failure("{}", "python_exec", _failed(_TABLE_ERROR.format(line=178)), seen)
    assert first == "{}"
    second = _with_repeated_failure("{}", "python_exec", _failed(_TABLE_ERROR.format(line=181)), seen)
    assert second.endswith("Same error as 1 earlier failed call in this run: IndexError: list index out of range (at pptx/table.py:40).")
    third = _with_repeated_failure("{}", "skill_invoke", _failed(_TABLE_ERROR.format(line=188), tool="skill_invoke"), seen)
    assert "Same error as 2 earlier failed calls in this run" in third
    other = _with_repeated_failure("{}", "python_exec", _failed('  File "/workdir/user.py", line 3\nTypeError: bad\n'), seen)
    assert other == "{}"
    # A success, or a tool that is not a script, is never annotated.
    ok = ToolInvocation(tool_name="python_exec", arguments={}, result={"ok": True, "result_json": {"ok": True, "stderr": _TABLE_ERROR}})
    assert _with_repeated_failure("{}", "python_exec", ok, seen) == "{}"
    assert _with_repeated_failure("{}", "bi_query", _failed(_TABLE_ERROR.format(line=1), tool="bi_query"), seen) == "{}"

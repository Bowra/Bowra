"""D17 — _map_code_block drops failed/scratch python_sandbox runs.

A failed sandbox run returns a clean {ok: false} (inv.error is None), so the top-of-loop
guard at mappers.py:305 misses it; the code_block mapper now gates on success so broken
scratch code doesn't leak into the canvas 代码 tab. Absence of ok/exit_code (synthetic
fixtures / older shapes) must NOT be false-skipped.
"""

from __future__ import annotations

from bowhead_core.canvas_composers.mappers import MapperInput, _map_code_block
from bowhead_core.types import IntentType, ToolInvocation


def _mi(invs: list[ToolInvocation]) -> MapperInput:
    return MapperInput(intent=IntentType.COMPUTE, question="", tool_invocations=invs, turn_id="t_d17")


def _sandbox_inv(result: dict) -> ToolInvocation:
    return ToolInvocation(
        tool_name="python_sandbox",
        arguments={"code": "df = pd.read_csv(INPUTS['rows']['path'])", "inputs": {}},
        result=result,
        duration_ms=5,
    )


def test_outer_ok_false_skipped() -> None:
    inv = _sandbox_inv({"ok": False, "result_json": {"ok": False, "exit_code": 1, "outputs": []}})
    assert _map_code_block(_mi([inv])) == []


def test_inner_ok_false_skipped() -> None:
    inv = _sandbox_inv({"result_json": {"ok": False, "outputs": []}})
    assert _map_code_block(_mi([inv])) == []


def test_nonzero_exit_code_skipped() -> None:
    inv = _sandbox_inv({"result_json": {"exit_code": 2, "outputs": []}})
    assert _map_code_block(_mi([inv])) == []


def test_successful_run_still_emitted() -> None:
    inv = _sandbox_inv({"ok": True, "result_json": {"ok": True, "exit_code": 0, "outputs": [{"artifact_id": "art_out", "name": "o.csv"}]}})
    out = _map_code_block(_mi([inv]))
    assert len(out) == 1
    assert out[0]["output_artifact_ids"] == ["art_out"]
    assert out[0]["output_artifacts"] == [{"artifact_id": "art_out", "filename": "o.csv"}]


def test_absent_ok_and_exit_not_skipped() -> None:
    # synthetic fixtures / older result shapes with no ok/exit_code must still emit.
    inv = _sandbox_inv({"stdout": "ok", "result_json": {"outputs": []}})
    assert len(_map_code_block(_mi([inv]))) == 1

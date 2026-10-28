"""followup-context-deck #5 — python_sandbox 假成功 + 404 input ref.

两个根因 (prod s_8a923ed27a, ref 5/6/9/10 全 404 但卡片显示"运行成功"):
- Fix A (404): LLM 把 <available_refs> simple-int id 塞进 inputs.artifact_id → runner
  host 侧 GET /api/v1/files/5 → 404. 用 ref allowlist 翻成真 id（2026-09-06 起实现在
  `bowhead_core.sandbox_inputs_normalize`，见下方 Fix A 段的说明）.
- Fix B (假成功): runner 把 input fetch 错误只拼 stderr 不翻 ok → user.py catch 后 exit 0 →
  假报成功. `_mark_input_errors` 在 input_errors 非空时翻 result.ok=False.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_fc5_runner_under_test", _RUNNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fc5_runner_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _load_runner()


# ═════════════════════════════════════════════════════════
# Fix B — _mark_input_errors: failed input fetch → fail loudly
# ═════════════════════════════════════════════════════════


def test_mark_input_errors_flips_ok_false(runner: ModuleType) -> None:
    # Simulate user.py catching a missing-input exception and exiting 0 → run_result.ok=True (old path falsely succeeded).
    result = {"ok": True, "exit_code": 0, "stderr": "", "error": None}
    runner._mark_input_errors(
        result,
        ["input '5' fetch failed: HTTP Error 404", "input '6' fetch failed: HTTP Error 404"],
    )
    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "input errors" in (result["error"] or "")
    assert "[runner] input errors" in result["stderr"]


def test_mark_input_errors_noop_when_empty(runner: ModuleType) -> None:
    # Normal runs without input errors are entirely unaffected.
    result = {"ok": True, "exit_code": 0, "stderr": "out", "error": None}
    runner._mark_input_errors(result, [])
    assert result == {"ok": True, "exit_code": 0, "stderr": "out", "error": None}


def test_mark_input_errors_preserves_existing_failure(runner: ModuleType) -> None:
    # Already-failed run: preserve exit_code / error, append only stderr, and keep ok=False.
    result = {"ok": False, "exit_code": 2, "stderr": "boom", "error": "user.py crashed"}
    runner._mark_input_errors(result, ["input '9' fetch failed: HTTP Error 404"])
    assert result["ok"] is False
    assert result["exit_code"] == 2
    assert result["error"] == "user.py crashed"
    assert "[runner] input errors" in result["stderr"]
    assert "boom" in result["stderr"]


# ═════════════════════════════════════════════════════════
# Fix A — simple-int → real art_id
#
# 2026-09-06 (sandbox-input-contract L2): moved the implementation from `tools._resolve_input_artifact_refs`
# to `bowhead_core.sandbox_inputs_normalize` (one implementation; the old copy **lacked the
# `kind == "data"` guard**, resolving chart simple refs to sec_* and then returning 404).
# Behavior is unchanged; move the tests with it. This relocates the feature rather than removing it.
# ═════════════════════════════════════════════════════════


class _StubAllowlist:
    """mimic RefAllowlist: known simple-int → real, else passthrough。

    ⚠ 比旧 stub 多一个 `kind_of` —— 新实现要判 `kind == "data"` 才放行。
    旧 stub 只有 `resolve`，那本身就是旧实现缺守卫的证据。
    """

    def __init__(self, mapping: dict[str, str], kinds: dict[int, str] | None = None) -> None:
        self._m = mapping
        self._k = kinds if kinds is not None else {int(k): "data" for k in mapping if str(k).isdigit()}

    def kind_of(self, simple_id: int) -> str | None:
        return self._k.get(simple_id)

    def resolve(self, x: str) -> str:
        return self._m.get(str(x), str(x))


def _ctx(allowlist):
    from types import SimpleNamespace

    return SimpleNamespace(_ν15_allowlist=allowlist, turn_id="t_x")


def _resolve(input_data: dict, allowlist) -> dict:
    """走新实现的等价入口（skill_invoke 形状）。"""
    from bowhead_core.sandbox_inputs_normalize import normalize_sandbox_inputs

    out, _ = normalize_sandbox_inputs(input_data.get("inputs"), _ctx(allowlist))
    return {**input_data, "inputs": out} if out is not None else input_data


def test_resolve_input_refs_translates_simple_int() -> None:
    allowlist = _StubAllowlist({"5": "art_2bbfdaab2430", "6": "art_efdeb2b82425"})
    input_data = {
        "inputs": {
            "rows": {"artifact_id": "5", "filename": "a.csv"},
            "more": {"artifact_id": "6"},
        }
    }
    out = _resolve(input_data, allowlist)
    assert out["inputs"]["rows"]["artifact_id"] == "art_2bbfdaab2430"
    assert out["inputs"]["more"]["artifact_id"] == "art_efdeb2b82425"
    assert out["inputs"]["rows"]["filename"] == "a.csv"  # Other fields stay unchanged.


def test_resolve_input_refs_passthrough_real_and_unknown() -> None:
    allowlist = _StubAllowlist({"5": "art_real"})
    input_data = {
        "inputs": {
            "a": {"artifact_id": "art_already_real"},  # Already a real ID → unchanged.
            "b": {"artifact_id": "999"},  # Unknown simple-int → preserve the value; runner reports failure as a fallback.
        }
    }
    out = _resolve(input_data, allowlist)
    assert out["inputs"]["a"]["artifact_id"] == "art_already_real"
    assert out["inputs"]["b"]["artifact_id"] == "999"


def test_resolve_input_refs_noop_without_allowlist() -> None:
    # allowlist=None (env BOWHEAD_REF_ALLOWLIST_INJECT=0) → no-op, unchanged.
    input_data = {"inputs": {"a": {"artifact_id": "5"}}}
    out = _resolve(input_data, None)
    assert out["inputs"]["a"]["artifact_id"] == "5"


def test_resolve_input_refs_handles_missing_inputs() -> None:
    # Missing inputs key (code only) → no error.
    input_data = {"code": "print(1)"}
    out = _resolve(input_data, _StubAllowlist({}))
    assert out.get("code") == "print(1)"


def test_chart_ref_is_not_resolved_into_a_non_file_id() -> None:
    """★ 新增：旧实现缺的那道守卫。

    `"12"` 若是「画布上第 12 张图」，resolve() 会给出 `sec_*` —— 那不是文件，
    塞进 artifact_id 后 runner 必然 404。新实现按 `kind` 拦住。
    """
    allowlist = _StubAllowlist({"12": "sec_abc123"}, kinds={12: "chart"})
    out = _resolve({"inputs": {"a": {"artifact_id": "12"}}}, allowlist)
    assert out["inputs"]["a"]["artifact_id"] == "12"

"""沙箱 `inputs` 入参归一 —— 救意图无歧义的形状，救不了的一律原样放行。

依据：`docs/sprints/sandbox-input-contract-2026-09-04/tech-design.md` §4.3。
116 三十天实测（preview 里看得见 inputs 的 284 次调用）：
对象+简单整数 id 66 次、值是裸规范串 62 次 —— 前者必然 404（沙箱入口没有
`add_chart` 那层解析），后者直接硬失败 `must be object, got str`。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.sandbox_inputs_normalize import (
    normalize_sandbox_inputs,
    normalize_sandbox_tool_args,
    salvage_misnested_sandbox_args,
)

REAL = "art_ebc3a9edc075"


class _Allowlist:
    """最小 ν1.5 allowlist 替身：只认 simple-id 17 → data / 12 → chart。"""

    def __init__(self, mapping=None, kinds=None):
        self._m = mapping or {"17": REAL}
        self._k = kinds or {17: "data"}

    def kind_of(self, simple_id: int):
        return self._k.get(simple_id)

    def resolve(self, aid: str):
        return self._m.get(str(aid))


def _ctx(allowlist=None, **kw):
    return SimpleNamespace(_ν15_allowlist=allowlist, turn_id="t_x", **kw)


# ── L2a: simple integer IDs ──────────────────────────────────────────────────
def test_simple_int_artifact_id_is_resolved() -> None:
    """`{"artifact_id": "17"}` —— 116 实测 66 次。不解析必然 404。"""
    out, shapes = normalize_sandbox_inputs({"rows": {"artifact_id": "17"}}, _ctx(_Allowlist()))
    assert out["rows"]["artifact_id"] == REAL
    assert shapes == ["simple_int_artifact_id"]


def test_canonical_artifact_id_is_untouched() -> None:
    """★ 负对照：已经写对的一个字节都不许动（否则"宽容"会变成"篡改"）。"""
    src = {"rows": {"artifact_id": REAL, "filename": "x.csv"}}
    out, shapes = normalize_sandbox_inputs(src, _ctx(_Allowlist()))
    assert out is src and shapes == []


def test_chart_kind_is_not_resolved_here() -> None:
    """`kind != "data"` 不放行 —— 「画布第 12 张图」是 sec_*，不是文件。

    （chart → 其背后 data artifact 的那一跳是 L2d，另一批做。）
    """
    al = _Allowlist(mapping={"12": "sec_abc"}, kinds={12: "chart"})
    out, shapes = normalize_sandbox_inputs({"rows": {"artifact_id": "12"}}, _ctx(al))
    assert out["rows"]["artifact_id"] == "12"
    assert shapes == []


def test_no_allowlist_leaves_everything_alone() -> None:
    """拿不到 allowlist（Lead 直调 / 老 harness）→ 原样放行，不猜。"""
    out, shapes = normalize_sandbox_inputs({"rows": {"artifact_id": "17"}}, _ctx(None))
    assert out["rows"]["artifact_id"] == "17"
    assert shapes == []


# ── L2a: bare string values ──────────────────────────────────────────────────
def test_bare_canonical_string_becomes_an_object() -> None:
    """`{"d": "art_xxx"}` —— 116 实测 62 次，原先硬失败 `must be object, got str`。"""
    out, shapes = normalize_sandbox_inputs({"d": REAL}, _ctx(_Allowlist()))
    assert out["d"] == {"artifact_id": REAL}
    assert shapes == ["bare_ref"]


def test_bare_simple_int_string_is_both_wrapped_and_resolved() -> None:
    out, shapes = normalize_sandbox_inputs({"d": "17"}, _ctx(_Allowlist()))
    assert out["d"] == {"artifact_id": REAL}
    assert shapes == ["bare_simple_int"]


@pytest.mark.parametrize("bad", [None, [], 3.5, True, ""])
def test_shapes_we_cannot_read_are_passed_through(bad) -> None:
    """★ 救不了的**原样放行**，让下游响亮报错。

    瞎猜会让模型拿到一份别人的数据 —— 比失败更糟。
    """
    src = {"d": bad}
    out, shapes = normalize_sandbox_inputs(src, _ctx(_Allowlist()))
    assert out == src and shapes == []


# ── L2b: nested-shape salvage ────────────────────────────────────────────────
def test_nested_input_is_salvaged_not_silently_dropped() -> None:
    """扁平入口收到 skill_invoke 的嵌套形状 —— 原先**静默丢弃**、INPUTS 为空。

    真机形态：提示（2026-09 前）教的是 `skill_invoke 的 input 里`，而拿到的工具是
    扁平 `python_exec`，模型照做就把 code/inputs 塞进顶层 `input`。
    """
    args = {"input": {"code": "print(1)", "inputs": {"rows": {"artifact_id": REAL}}}}
    out, shape = salvage_misnested_sandbox_args(args)
    assert out["code"] == "print(1)"
    assert out["inputs"] == {"rows": {"artifact_id": REAL}}
    assert "input" not in out
    assert shape and shape.startswith("nested_input_")


def test_correct_flat_call_is_byte_identical() -> None:
    """★ 负对照：写对的调用不许被抢救逻辑碰到。"""
    args = {"code": "print(1)", "inputs": {"rows": {"artifact_id": REAL}}}
    out, shape = salvage_misnested_sandbox_args(args)
    assert out is args and shape is None


def test_top_level_wins_over_nested_when_both_present() -> None:
    """顶层已有的键不被覆盖 —— 抢救只补缺，不改写模型明确写在顶层的东西。"""
    args = {"code": "top", "input": {"code": "nested", "inputs": {"a": {"artifact_id": REAL}}}}
    out, _ = salvage_misnested_sandbox_args(args)
    assert out["code"] == "top"
    assert out["inputs"] == {"a": {"artifact_id": REAL}}


def test_salvage_hoists_intent_too() -> None:
    """`intent` 是公开输入字段 ⇒ 也在抢救白名单里。

    白名单漏一个字段，模型嵌错时那个字段就在归一后的 args 里消失——这仓踩过好几次。
    （来源标签本身不依赖这条：它读的是**未归一**的原始 args，两种 holder 都认。）
    """
    args = {"input": {"code": "print(1)", "intent": "各省排名与 z 值", "inputs": {"rows": {"artifact_id": REAL}}}}
    out, shape = salvage_misnested_sandbox_args(args)
    assert out["intent"] == "各省排名与 z 值"
    assert shape and "intent" in shape


def test_unrelated_input_key_is_left_alone() -> None:
    """`input` 里没有 code/inputs ⇒ 不是错嵌的沙箱调用，别动。"""
    args = {"code": "x", "input": {"something_else": 1}}
    out, shape = salvage_misnested_sandbox_args(args)
    assert out is args and shape is None


def test_intent_coverage_is_counted_for_both_entries(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`intent` 是 schema-required 但运行时 fail-open ⇒「模型到底填不填」只能靠计数看见。

    看不见就等于没上线：缺填率是决定「要不要升级成硬拒」的唯一依据。
    """
    seen: list[tuple[str, dict[str, str]]] = []

    from bowhead_core import metrics as _m

    monkeypatch.setattr(_m, "inc", lambda name, labels=None, **_kw: seen.append((name, labels or {})))

    ctx = _ctx(_Allowlist())
    normalize_sandbox_tool_args("python_exec", {"code": "c", "intent": "算了个东西"}, ctx)
    normalize_sandbox_tool_args("python_exec", {"code": "c"}, ctx)
    normalize_sandbox_tool_args(
        "skill_invoke",
        {"name": "python_sandbox", "input": {"code": "c", "intent": "算了个东西"}},
        ctx,
    )
    normalize_sandbox_tool_args("skill_invoke", {"name": "python_sandbox", "input": {"code": "c"}}, ctx)

    counted = [labels for name, labels in seen if name == "bowhead_sandbox_intent_declared_total"]
    assert [(c["tool"], c["filled"]) for c in counted] == [
        ("python_exec", "1"),
        ("python_exec", "0"),
        ("skill_invoke", "1"),
        ("skill_invoke", "0"),
    ], counted


def test_intent_counted_after_salvage_not_before(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """填了但嵌错了 ⇒ 不能记成「没填」，否则缺填率读数会把形状问题算成模型不配合。"""
    seen: list[dict[str, str]] = []

    from bowhead_core import metrics as _m

    monkeypatch.setattr(
        _m,
        "inc",
        lambda name, labels=None, **_kw: seen.append(labels or {}) if name == "bowhead_sandbox_intent_declared_total" else None,
    )
    normalize_sandbox_tool_args(
        "python_exec",
        {"input": {"code": "c", "intent": "算了个东西", "inputs": {}}},
        _ctx(_Allowlist()),
    )
    assert [c["filled"] for c in seen] == ["1"], seen


# ── Entry dispatch ──────────────────────────────────────────────────────────
def test_flat_entry_end_to_end() -> None:
    args = {"code": "c", "inputs": {"rows": "17"}}
    out = normalize_sandbox_tool_args("python_exec", args, _ctx(_Allowlist()))
    assert out["inputs"]["rows"] == {"artifact_id": REAL}


def test_skill_invoke_entry_normalizes_the_nested_inputs() -> None:
    args = {"name": "python_sandbox", "script": "runner.py", "input": {"code": "c", "inputs": {"rows": "17"}}}
    out = normalize_sandbox_tool_args("skill_invoke", args, _ctx(_Allowlist()))
    assert out["input"]["inputs"]["rows"] == {"artifact_id": REAL}
    assert out["input"]["code"] == "c"


def test_skill_invoke_for_another_skill_is_untouched() -> None:
    """★ 只管沙箱那个 skill —— 别的 skill 的 input 形状与我们无关。"""
    args = {"name": "canvas-pptx", "input": {"inputs": {"rows": "17"}}}
    out = normalize_sandbox_tool_args("skill_invoke", args, _ctx(_Allowlist()))
    assert out is args


def test_non_sandbox_tool_is_untouched() -> None:
    args = {"data_artifact_id": "17"}
    assert normalize_sandbox_tool_args("add_chart", args, _ctx(_Allowlist())) is args


# ── Wiring: pure-function correctness does not prove handler invocation ──────
#
# This sprint already hit two traps (`_month_coverage_note` unwired with 47 checks still green;
# `_hint_missing_file` effective only for FileNotFoundError). Get handler from **the actual wrapper**
# and assert normalization occurs before entering the sandbox.


@pytest.mark.asyncio
async def test_normalization_is_wired_into_the_registered_flat_tool() -> None:
    """★ 从**真注册路径**取 handler，断言归一发生在进入沙箱之前。

    ⚠ 第一版这条是自己 new 一个包装器再调 —— 删掉 skill_bridge 的接线**照样绿**
    （只有源码扫描那条红）。那就是「只测 helper 没测调用点」，本 sprint 已经栽过。
    现在走 `register_skill_tools`：删接线 → 本条必红。
    """
    from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools
    from bowhead_core.tool_registry import build_default_registry

    reg = build_default_registry()
    cat, rt = build_default_skill_stack()
    register_skill_tools(reg, cat, rt)

    spec = reg.get("python_exec")
    assert spec is not None, "扁平沙箱入口没注册（FEATURE_PYTHON_SANDBOX_FLAT_TOOL 关了？）"

    seen: dict = {}

    async def _spy(args, ctx):  # Replace actual sandbox execution; inspect pre-handler shape only
        seen.update(args)
        return {"ok": True}

    # Use closure `__qualname__`, **not** object inequality with the bare handler:
    # `build_skill_tools` creates new closures each call, making identity unequal and passing whether wrapped or not.
    # The first version did exactly that; mutation testing **did not fail** (only the source-scan test failed).
    assert "_wrap_sandbox_inputs_normalization" in spec.handler.__qualname__, (
        f"扁平沙箱入口的 handler 没被包装 —— 归一没接上（qualname={spec.handler.__qualname__}）"
    )

    # Also prove the wrapper actually normalizes (behavior).
    from bowhead_core.skill_bridge import _wrap_sandbox_inputs_normalization

    await _wrap_sandbox_inputs_normalization("python_exec", _spy)(
        {"code": "c", "inputs": {"rows": {"artifact_id": "17"}}},
        _ctx(_Allowlist()),
    )
    assert seen["inputs"]["rows"]["artifact_id"] == REAL

    # skill_invoke must also be wrapped (not just one entry).
    si = reg.get("skill_invoke")
    assert si is not None
    assert "_wrap_sandbox_inputs_normalization" in si.handler.__qualname__, "skill_invoke 入口没被包装"


@pytest.mark.asyncio
async def test_wrapper_never_breaks_a_call_when_normalization_raises() -> None:
    """归一是尽力而为 —— 它自己炸了也不许把正常调用打挂。"""
    from bowhead_core.skill_bridge import _wrap_sandbox_inputs_normalization

    class _Exploding:
        def kind_of(self, _):
            raise RuntimeError("boom")

        def resolve(self, _):
            raise RuntimeError("boom")

    called: dict = {}

    async def _fake_handler(args, ctx):
        called["args"] = args
        return {"ok": True}

    wrapped = _wrap_sandbox_inputs_normalization("python_exec", _fake_handler)
    out = await wrapped({"code": "c", "inputs": {"rows": {"artifact_id": "17"}}}, _ctx(_Exploding()))
    assert out == {"ok": True}
    assert called["args"]["inputs"]["rows"]["artifact_id"] == "17"  # Pass through unchanged


def test_registered_sandbox_tools_are_wrapped() -> None:
    """守卫：两个沙箱入口都必须被包上，别只包一个。"""
    import inspect

    from bowhead_core.skill_bridge import register_skill_tools

    src = inspect.getsource(register_skill_tools)
    assert "_wrap_sandbox_inputs_normalization" in src
    assert "FLAT_SANDBOX_TOOL" in src and "SKILL_INVOKE_TOOL" in src


# ── Hardcoded code paths → bind as inputs (116 `t_239e8b6cf3dc` / `t_6acd16abddee`: 2–3 wasted runs per turn) ──
PRIOR = "art_0123456789ab"


class _Store:
    """本 turn 已落库的沙箱产物（按文件名）。"""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.asked: list[tuple[str, list[str]]] = []

    async def find_turn_artifact_ids_by_filename(self, turn_id: str, names: list[str]) -> dict[str, str]:
        self.asked.append((turn_id, list(names)))
        return {n: self.files[n] for n in names if n in self.files}


async def _run_wrapped(args: dict, store: _Store | None = None) -> tuple[dict, dict]:
    from bowhead_core.skill_bridge import _wrap_sandbox_inputs_normalization

    seen: dict = {}

    async def _spy(a, ctx):
        seen.update(a)
        return {"ok": True}

    out = await _wrap_sandbox_inputs_normalization("python_exec", _spy)(args, _ctx(None, artifact_store=store))
    return seen, out


@pytest.mark.asyncio
@pytest.mark.parametrize("literal", ["inputs/art_ebc3a9edc075.csv", "artifacts/art_ebc3a9edc075.csv", "/data/art_ebc3a9edc075.csv"])
async def test_artifact_id_path_literal_is_bound_to_an_input(literal: str) -> None:
    seen, out = await _run_wrapped({"code": f"import pandas as pd\ndf = pd.read_csv('{literal}')"})
    assert seen["inputs"] == {REAL: {"artifact_id": REAL}}
    assert f"pd.read_csv(INPUTS['{REAL}']['path'])" in seen["code"]
    assert literal in out["input_autobind_note"]


@pytest.mark.asyncio
async def test_artifact_path_reuses_the_key_it_was_already_declared_under() -> None:
    seen, _ = await _run_wrapped({"code": f"pd.read_csv('inputs/{REAL}.csv')", "inputs": {"d26": {"artifact_id": REAL}}})
    assert seen["inputs"] == {"d26": {"artifact_id": REAL}}
    assert seen["code"] == "pd.read_csv(INPUTS['d26']['path'])"


@pytest.mark.asyncio
async def test_prior_run_file_is_bound_from_this_turns_artifacts() -> None:
    store = _Store({"category_metrics.csv": PRIOR})
    seen, out = await _run_wrapped({"code": "m = pd.read_csv('category_metrics.csv')"}, store)
    assert store.asked == [("t_x", ["category_metrics.csv"])]
    assert seen["inputs"] == {"category_metrics": {"artifact_id": PRIOR}}
    assert seen["code"] == "m = pd.read_csv(INPUTS['category_metrics']['path'])"
    assert PRIOR in out["input_autobind_note"]


@pytest.mark.asyncio
async def test_file_the_code_writes_itself_is_left_alone() -> None:
    """同一段代码先写后读 = 读的是本次新写的那份，绝不能换成上次的产物。"""
    store = _Store({"category_metrics.csv": PRIOR})
    code = "df.to_csv('category_metrics.csv', index=False)\nm = pd.read_csv('category_metrics.csv')"
    seen, out = await _run_wrapped({"code": code}, store)
    assert seen["code"] == code
    assert "inputs" not in seen and "input_autobind_note" not in out
    assert store.asked == []


@pytest.mark.asyncio
async def test_correct_calls_are_untouched() -> None:
    """负对照：已写对的调用（下标取 path、声明过的 key）一个字节都不改，也不查库。"""
    store = _Store({"rows.csv": PRIOR})
    args = {"code": f"df = pd.read_csv(INPUTS['{REAL}']['path'])\nx = INPUTS['rows']['path']", "inputs": {REAL: {"artifact_id": REAL}}}
    seen, out = await _run_wrapped(dict(args), store)
    assert seen == args
    assert "input_autobind_note" not in out


@pytest.mark.asyncio
async def test_unknown_filename_is_left_for_the_downstream_error() -> None:
    """本 turn 没有这份文件 → 不猜，原样放行（下游 FileNotFoundError 会附可用清单）。"""
    seen, out = await _run_wrapped({"code": "pd.read_csv('missing.csv')"}, _Store({}))
    assert seen["code"] == "pd.read_csv('missing.csv')"
    assert "input_autobind_note" not in out


@pytest.mark.asyncio
async def test_skill_invoke_shape_is_bound_inside_input() -> None:
    from bowhead_core.skill_bridge import _wrap_sandbox_inputs_normalization

    seen: dict = {}

    async def _spy(a, ctx):
        seen.update(a)
        return {"ok": True}

    await _wrap_sandbox_inputs_normalization("skill_invoke", _spy)(
        {"name": "python_sandbox", "script": "runner.py", "input": {"code": f"pd.read_csv('inputs/{REAL}.csv')"}},
        _ctx(None),
    )
    assert seen["input"]["inputs"] == {REAL: {"artifact_id": REAL}}
    assert seen["input"]["code"] == f"pd.read_csv(INPUTS['{REAL}']['path'])"
    assert seen["script"] == "runner.py"

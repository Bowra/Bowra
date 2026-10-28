"""Render the shared sandbox input contract for both supported entry points.

python_exec accepts {code, inputs}; skill_invoke accepts input: {code, inputs}.
Inputs are always siblings of code, so relative wording remains correct when
the entry point is unknown. BI hints, runtime errors, and tool schemas render
this same contract. The separate runner process receives the rendered skeleton
through an internal control field rather than maintaining a second renderer."""

from __future__ import annotations

from collections.abc import Iterable

#: Flat-entry tool name. Matches `_FLAT_SANDBOX_TOOL` in `tools.py` / `resource_access.py` / `subagent.py`;
#: those are constants for their respective uses, while this module handles only the call shape.
FLAT_SANDBOX_TOOL = "python_exec"
#: Legacy entry: `skill_invoke(name="python_sandbox", script="runner.py", input={...})`.
SKILL_INVOKE_TOOL = "skill_invoke"
SANDBOX_SKILL_NAME = "python_sandbox"
SANDBOX_SCRIPT = "runner.py"

#: Example key. Use `rows` because BI result hints have always used it and the model already follows that convention
#: (116 observations over thirty days: `INPUTS['rows']` appeared 181 times, three times the runner-up).
DEFAULT_INPUT_KEY = "rows"

Entry = str | None


def sandbox_entry_for(tool_names: Iterable[str] | None) -> Entry:
    """从"调用方持有的工具名"判沙箱入口。判不出返 `None`（⇒ 用保底说法）。

    传 `None` 或空集合都返 `None` —— **不猜**。宁可给一句两条路都对的保底，
    也不要给一半调用方一个错的绝对层级。
    """
    if not tool_names:
        return None
    names = set(tool_names)
    if FLAT_SANDBOX_TOOL in names:
        return FLAT_SANDBOX_TOOL
    if SKILL_INVOKE_TOOL in names:
        return SKILL_INVOKE_TOOL
    return None


def sandbox_call_skeleton(
    entry: Entry,
    *,
    key: str = DEFAULT_INPUT_KEY,
    artifact_id: str = "art_...",
) -> str:
    """渲染该入口的调用骨架。`entry=None` → 入口无关的保底说法。

    保底说法**必须**是相对位置（「与 `code` 同级」），不能是绝对层级。
    """
    decl = f'"{key}": {{"artifact_id": "{artifact_id}"}}'
    if entry == FLAT_SANDBOX_TOOL:
        return f'{FLAT_SANDBOX_TOOL}(code="<python>", inputs={{{decl}}})'
    if entry == SKILL_INVOKE_TOOL:
        return f'{SKILL_INVOKE_TOOL}(name="{SANDBOX_SKILL_NAME}", script="{SANDBOX_SCRIPT}", input={{"code": "<python>", "inputs": {{{decl}}}}})'
    # Fallback: return **code only**. Emit relative-position guidance separately through `sandbox_position_note()`;
    # adding it here lets callers wrap it in backticks, and nested backticks break the code span (observed in rendering).
    return f"inputs={{{decl}}}"


def sandbox_position_note(entry: Entry) -> str:
    """入口判不出时补的相对位置说明；入口已知时为空串（骨架本身已说明层级）。

    ⚠ 必须讲**相对位置**不能讲绝对层级：`inputs` 在扁平入口是顶层、在 skill_invoke
    里嵌一层，「顶层」/「input 里」各对一半调用方是错的，只有「与 code 同级」都成立。
    """
    return "" if entry in (FLAT_SANDBOX_TOOL, SKILL_INVOKE_TOOL) else "（`inputs` 与 `code` 同级）"


def sandbox_read_snippet(key: str = DEFAULT_INPUT_KEY) -> str:
    """代码里怎么取回来。与骨架成对出现——只给一半模型照样跑不通。"""
    return f"INPUTS['{key}']['path']"


def sandbox_inputs_hint(
    entry: Entry,
    *,
    key: str = DEFAULT_INPUT_KEY,
    artifact_id: str = "art_...",
    row_count: int | None = None,
    short: bool = False,
) -> str:
    """「怎么把这份数据传进沙箱」的完整提示，供 BI 工具结果附带。

    `short=True` 是**预算裁剪档**：只保留骨架 + 取法（约 40 token）。
    整句被裁掉换来的是整轮计算失败，所以宁可留短版也不要留一句没有形状的话
    （`tool_result_budget` 原先直接 pop 掉整条）。
    """
    skeleton = sandbox_call_skeleton(entry, key=key, artifact_id=artifact_id)
    note = sandbox_position_note(entry)
    read = sandbox_read_snippet(key)
    if short:
        return f"传给 python 沙箱：`{skeleton}`{note}，代码里 `pd.read_csv({read})`。"
    scale = f"读全量 {row_count} 行. " if row_count is not None else ""
    return (
        f"把这份数据传给 python 沙箱时：`{skeleton}`{note}，代码里 `pd.read_csv({read})` {scale}"
        "**只要 artifact_id**——文件名后端自己补，别复述（长文件名逐字打进 JSON "
        "是流式 tool-args 写崩的高发点，实测过）. "
        "**不要**自己把 rows 逐字 type 进 code（烧 token + 易截断）."
    )

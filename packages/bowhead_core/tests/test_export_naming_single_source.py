"""导出文件名必须**只有一个**推导来源（116 prod 反复实证）。

这条闸存在的理由：同一件事在仓库里有**三个**实现点，我分三次才修全，每次都以为修完了。

| 实现点                                   | 修复时间   | 当时的表现                                   |
| ---------------------------------------- | ---------- | -------------------------------------------- |
| `subagent.py`（skill 导出路）            | 1.0.79 前  | 所有导出都叫 `Bowhead Canvas.docx`           |
| `canvas_export.py` ×2（legacy job 路）   | 1.0.79     | 都叫 `canvas.docx` / `canvas.pptx`           |
| `export_snapshot.py`（**当前真跑的**）   | 1.1.26     | 都叫 `Bowhead Canvas.xlsx`——而画布有 h1     |

根因每次都一样：`canvas.title` 只有用户手动重命名画布才有值，自动产出恒 NULL，
于是直接 `canvas.title or "…"` 的地方必然掉进兜底。真标题一直在 h1 段里。

所以这里钉的不是某一次修复，而是**「不许再出现第四个自己推导标题的地方」**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "bowhead_core"

# Modules deciding user-visible filenames. New export paths **must** be added here (failure to add one means it constructs its own names).
_NAMING_MODULES = ("canvas_export.py", "export_snapshot.py")

# Own-name construction shape: fall back directly from canvas.title to a literal.
_BAD_SHAPES = ('canvas.title or "Bowhead Canvas"', "canvas.title or 'Bowhead Canvas'", "canvas.title or 'canvas'", 'canvas.title or "canvas"')


@pytest.mark.parametrize("mod", _NAMING_MODULES)
def test_naming_modules_use_the_shared_helper(mod: str) -> None:
    """★ 正对照：这些模块必须**真的**引用共享 helper。

    没有这一条，下面的「不含坏形状」断言会在模块被改名/搬走时静默变成恒真。
    """
    text = (_SRC / mod).read_text(encoding="utf-8")
    assert "report_title_from_sections" in text, f"{mod} 没走共享命名 helper"


@pytest.mark.parametrize("mod", _NAMING_MODULES)
def test_no_module_derives_the_export_name_itself(mod: str) -> None:
    """★ 决定**文件名**的地方不许再出现 `canvas.title or <字面量>` 这种自造兜底。

    留了一个显式例外：snapshot payload 里记录画布自身 title 的那处是审计元数据，
    不参与命名——它带 `# naming-exempt` 标记。
    """
    lines = (_SRC / mod).read_text(encoding="utf-8").splitlines()

    def _exempt(idx: int) -> bool:
        # Exemption markers may be on this line or either of the preceding two lines (Python comments commonly sit above code).
        return any("naming-exempt" in lines[j] for j in range(max(0, idx - 3), idx))

    offenders = [(i, ln.strip()) for i, ln in enumerate(lines, 1) if any(bad in ln for bad in _BAD_SHAPES) and not _exempt(i)]
    assert not offenders, f"{mod} 出现了第 N 个自造文件名的地方：{offenders}"

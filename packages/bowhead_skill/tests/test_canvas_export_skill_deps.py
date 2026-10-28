"""导出 skill 的运行时依赖必须在 `bowhead_skill` 里声明齐。

背景（116 v1.1.38 实测）：非 `python_sandbox` 的 skill 子进程跑在 **app 容器**里
（`bowhead_skill/runtime.py`），而 app 依赖里**漏了 matplotlib** ⇒ `canvas-pdf` 的
`_render_chart_png` 里 `import matplotlib` 恒失败 ⇒ 返回 None ⇒ **每一张图都降级成数据表**。
不是偶发，是 100% 必然：镜像内实测 `matplotlib` ModuleNotFoundError，而 reportlab /
openpyxl 都在——所以文件出得来、只有图出不来，用户拿到的 PDF 里一张图都没有。

☠ 这事**曾经被发现过**：`canvas-docx` 的 docstring 里写着「app 容器里没有它」，
写那个 skill 的人绕开了，但 canvas-pdf 没修、也没人告诉用户。知识留在注释里没变成修复。

本文件把「脚本 import 什么」和「依赖声明了什么」钉在一起，防止再漏第二次。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]
CATALOG = PKG_ROOT / "catalog"
PYPROJECT = PKG_ROOT / "pyproject.toml"

# Top-level third-party import → dependency name (only those actually used by export skills).
_IMPORT_TO_DIST = {
    "matplotlib": "matplotlib",
    "reportlab": "reportlab",
    "openpyxl": "openpyxl",
    "pptx": "python-pptx",
}


def _declared() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    out = set()
    for raw in data["project"]["dependencies"]:
        out.add(re.split(r"[<>=!~\[ ]", raw, maxsplit=1)[0].strip().lower())
    return out


def _export_skill_scripts() -> list[Path]:
    return sorted(CATALOG.glob("canvas-*/scripts/render_from_canvas.py"))


def test_export_skill_scripts_exist() -> None:
    """锚点自检——脚本没了的话下面的断言会空转变成假绿。"""
    scripts = _export_skill_scripts()
    assert len(scripts) >= 3, f"只找到 {len(scripts)} 个导出 skill 脚本"


@pytest.mark.parametrize("script", _export_skill_scripts(), ids=lambda p: p.parent.parent.name)
def test_every_import_is_declared(script: Path) -> None:
    """脚本里 import 的第三方库，必须在 bowhead_skill 依赖里声明。"""
    src = script.read_text(encoding="utf-8")
    declared = _declared()
    for module, dist in _IMPORT_TO_DIST.items():
        # Recognize `import x` / `from x import ...`, including indentation (these scripts import lazily inside functions).
        if not re.search(rf"^\s*(?:import|from)\s+{re.escape(module)}\b", src, re.MULTILINE):
            continue
        assert dist in declared, (
            f"{script.parent.parent.name} 的脚本 import 了 {module}，但 bowhead_skill 依赖里没有 {dist}"
            f"——它跑在 app 容器里，缺依赖不会报错，只会走静默降级分支"
        )


def test_matplotlib_declared() -> None:
    """单列一条：这就是那次事故本身，别被上面的参数化淹没。"""
    assert "matplotlib" in _declared(), "canvas-pdf 画图要它；漏了就是每张图都变数据表"

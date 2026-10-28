"""目标名解析的 gateway 转发（实现已于 2026-09-13 移到 `bowhead_core.feedback_target_resolver`）。

移动原因：自动进货口归 memory 线后，core 侧的进货路径也要解析目标名，而 core 不能 import gateway。
本模块只做再导出，老调用点（`main.py` 的工具 provider）不动。
"""

from __future__ import annotations

from bowhead_core.feedback_target_resolver import AmbiguousTarget, resolve_feedback_target

__all__ = ["AmbiguousTarget", "resolve_feedback_target"]

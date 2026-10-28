"""交付物文件名：从报告自身的标题推导，而不是写死 ``Bowhead Canvas``。

dev 1.0.77 dogfood 的 P2-a/P2-b：同一个库里躺着 3 份同名 ``Bowhead Canvas.docx``，
用户既分不清哪份是哪份，答案里说的名字（模型照抄 sandbox 的 ``output.docx``）又跟真实
文件名对不上——按答案里的名字去下载目录里找，找不到。

根因不是"没人想过取名"，而是**唯一的取名依据选错了**：登记处用 ``canvas.title``，
而 `canvas_store.set_title` 全仓只有一个调用方——用户在前端手动重命名画布的路由。
自动分析产出的画布 title 恒为 NULL ⇒ 每次都掉进 ``Bowhead Canvas`` 兜底。
真正的报告标题一直都在，在 ``h1`` 段里（导出器渲染的就是它）。

本模块是这条推导的**单一实现**，登记侧（`bowhead_core.subagent`）与模型可见侧
（`bowhead_skill.tools`）都调它 —— 两边各写一份必然漂移，而漂移的表现恰好就是
P2-a（答案里的名字 ≠ 真实名字）。
"""

from __future__ import annotations

import re
from typing import Any

# Fallback name: preserve the historical value so existing users do not think their files disappeared.
FALLBACK_BASENAME = "Bowhead Canvas"

# Cross-platform forbidden filename characters + control characters. Windows is stricter than POSIX; use the stricter set.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WS = re.compile(r"\s+")
# Reserved Windows device names (case-insensitive, even with extensions); files using these names cannot be opened on Windows.
_RESERVED = frozenset({"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)})
# 60 characters: comfortably fits Windows MAX_PATH 260 after download-directory depth and extensions; 60 Chinese characters identify a title sufficiently.
MAX_BASENAME = 60


def sanitize_basename(title: str | None) -> str | None:
    """标题 → 可安全落盘的文件名主干。不可用时返回 None（由调用方兜底）。"""
    if not title:
        return None
    name = _UNSAFE.sub(" ", str(title))
    name = _WS.sub(" ", name).strip()
    # Windows silently strips leading/trailing dots and spaces, making the filename differ from its registered value.
    name = name.strip(". ")
    if not name:
        return None
    if len(name) > MAX_BASENAME:
        name = name[:MAX_BASENAME].rstrip(". ")
    if not name or name.lower() in _RESERVED:
        return None
    return name


def report_title_from_sections(sections: Any, canvas_title: str | None = None) -> str | None:
    """报告标题：**第一个 h1 段**优先，其次画布 title。

    h1 优先于 canvas.title 是有意的：canvas.title 只有用户手动重命名才有值，
    而 h1 是 compose 阶段由报告自己写下的标题——它才是这份交付物的名字。

    ``sections`` 可以是 ``Section`` 行（``.kind`` / ``.payload``）或已打平的 dict，
    两种形态在仓库里都存在，这里一并吃下。
    """
    for s in sections or []:
        kind = s.get("kind") if isinstance(s, dict) else getattr(s, "kind", None)
        if kind != "h1":
            continue
        if isinstance(s, dict):
            payload = s.get("payload") if isinstance(s.get("payload"), dict) else s
        else:
            payload = getattr(s, "payload", None) or {}
        text = payload.get("text") if isinstance(payload, dict) else None
        cleaned = sanitize_basename(text)
        if cleaned:
            return cleaned
    return sanitize_basename(canvas_title)


def export_filename(title: str | None, ext: str) -> str:
    """``(标题, '.docx') → '标题.docx'``；标题不可用时回到 ``Bowhead Canvas.docx``。"""
    base = sanitize_basename(title) or FALLBACK_BASENAME
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"{base}{ext}"

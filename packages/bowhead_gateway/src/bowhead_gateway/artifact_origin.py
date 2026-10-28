"""source/kind → 用户面 origin 枚举 —— 全 gateway 唯一实现.

file-management-v2 P0 收口：此逻辑曾在 routes/files.py、routes/canvas.py、
routes/sessions.py 各有一份手抄拷贝，P0 只修了 files.py 一份——canvas
source-panel 与 Data tab 仍把 AI 生成背景图标成「上传」。第 3 份拷贝出现
即触发统一（禁止第 4 份手抄，guard: test_files_origin_guard.py）。

origin 枚举与 i18n `dataTab.origin.*` 键一一对应：
    query / compute / upload / export / web / deck / other

优先级（与 asset model 语义一致）：
    1. 精确 producer_kind/source map（如何产生）
    2. web_clip/user_upload 等无歧义 kind fallback
    3. deliverable → other（只说明交付，不说明由 Deck 产生）
    4. 未知 source → "other"（不得伪装成用户上传）
"""

from __future__ import annotations

SOURCE_TO_ORIGIN: dict[str, str] = {
    "upload": "upload",
    "user_upload": "upload",
    "kb_upload": "upload",
    "team_branding": "upload",
    "canvas_export": "export",
    "canvas_export_clone": "export",
    "canvas_export_skill": "export",
    # Bowra document Skill deliverables use the same delivery path as canvas_export_skill.
    "community_document_skill": "export",
    "design_flatten": "export",
    "python_sandbox": "compute",
    "sandbox_output": "compute",
    "chart_calculation": "compute",
    # AI-generated assets (design backgrounds or explicit image deliverables) retain generation-based classification.
    "image_generate": "compute",
    "bi_fetch_rows": "query",
    "template_clone_section_data": "query",
    "web_search": "web",
    "web_fetch": "web",
    "web_extract": "web",
    "web_crawl": "web",
    "web_deep_research": "web",
    "consultant_deck": "deck",
    "kb_bundle_upload": "upload",
}

VALID_ORIGINS: frozenset[str] = frozenset({"query", "compute", "upload", "export", "web", "deck", "other"})


def origin_for(kind: str | None, source: str, producer_kind: str | None = None) -> str:
    """Resolve creation mechanism; broad delivery kind never masks producer."""
    for candidate in (producer_kind, source):
        if candidate and candidate in SOURCE_TO_ORIGIN:
            return SOURCE_TO_ORIGIN[candidate]
    if kind == "web_clip":
        return "web"
    if kind == "user_upload":
        return "upload"
    if kind == "sandbox_output":
        return "compute"
    return "other"

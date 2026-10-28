"""Sprint chat-redesign-2026-05-21 Phase 1.1 — L3 picker candidate resolution.

When chat_send arrives with empty message + attachments, gateway calls
`candidates_from_attachments` to produce a 2-3 item list (e.g.
["做个分析", "收进知识库", "照这个格式做新的"]) that gets passed to Lead via
`TurnRequest.l3_picker_hint`. Lead then emits clarification_ask with these
candidates instead of guessing intent.

Resolution: artifact_id → mime type → candidate list (table lookup).
Multi-attachment goes to the `_multi` bucket regardless of types.

The candidates are user-language strings — they show up verbatim in the FE
HITLPickerCard. Keep them short + action-oriented.
"""

from __future__ import annotations

from typing import Any

# zh-CN short action labels — shown verbatim in HITLPickerCard.
CANDIDATES_BY_MIME: dict[str, list[str]] = {
    "csv": ["做个分析", "收进知识库", "照这个格式做新的"],
    "xlsx": ["做个分析", "收进知识库", "照这个格式做新的"],
    "xls": ["做个分析", "收进知识库", "照这个格式做新的"],
    "pdf": ["总结一下", "找特定内容", "收进知识库"],
    "docx": ["总结一下", "找特定内容", "收进知识库"],
    "doc": ["总结一下", "找特定内容", "收进知识库"],
    "pptx": ["总结一下", "当报告模板", "收进知识库"],
    "ppt": ["总结一下", "当报告模板", "收进知识库"],
    "image": ["看图说话", "把文字抄出来", "收进知识库"],
    "url": ["看看网页讲啥", "抓里面的数据", "收进知识库"],
    "_multi": ["全部分析 (文件+网页)", "全部收进知识库", "分别处理"],
    "_unknown": ["看看里面是什么", "收进知识库", "我自己说"],
}


def _mime_to_short(mime: str, filename: str = "") -> str:
    """Reduce a full mime string to a CANDIDATES_BY_MIME key.

    Falls back to filename suffix when mime is generic (application/octet-stream
    or empty), since many uploads land without a meaningful mime header.
    """
    m = (mime or "").lower()
    f = (filename or "").lower()
    if "csv" in m or f.endswith(".csv"):
        return "csv"
    if "spreadsheetml" in m or m.endswith("vnd.ms-excel") or f.endswith((".xlsx", ".xls")):
        return "xlsx" if f.endswith(".xlsx") or "openxml" in m else "xls"
    if m == "application/pdf" or f.endswith(".pdf"):
        return "pdf"
    if "wordprocessingml" in m or f.endswith(".docx"):
        return "docx"
    if m == "application/msword" or f.endswith(".doc"):
        return "doc"
    if "presentationml" in m or f.endswith(".pptx"):
        return "pptx"
    if m == "application/vnd.ms-powerpoint" or f.endswith(".ppt"):
        return "ppt"
    if m.startswith("image/"):
        return "image"
    if m.startswith("text/uri-list") or f.startswith(("http://", "https://")):
        return "url"
    return "_unknown"


async def candidates_from_attachments(
    attachment_ids: list[str],
    *,
    artifact_store: Any,
) -> list[str]:
    """Resolve artifact_ids → mime types → candidate list.

    Multi-attachment → CANDIDATES_BY_MIME["_multi"] (one bucket regardless of
    mix). Single attachment → mime-derived bucket. Unknown / unfetchable →
    `_unknown` bucket (3 generic options including "I'll type it myself").

    `artifact_store` must expose `async get(artifact_id) -> Artifact | None`
    with `.mime` and `.filename` attributes (matches existing ArtifactStore).
    """
    if not attachment_ids:
        return CANDIDATES_BY_MIME["_unknown"]
    if len(attachment_ids) > 1:
        return CANDIDATES_BY_MIME["_multi"]
    # Single attachment — look up mime
    try:
        art = await artifact_store.get(attachment_ids[0])
    except Exception:
        return CANDIDATES_BY_MIME["_unknown"]
    if art is None:
        return CANDIDATES_BY_MIME["_unknown"]
    key = _mime_to_short(
        getattr(art, "mime", "") or "",
        getattr(art, "filename", "") or "",
    )
    return CANDIDATES_BY_MIME.get(key, CANDIDATES_BY_MIME["_unknown"])

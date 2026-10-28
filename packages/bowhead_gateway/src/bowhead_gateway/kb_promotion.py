"""file-management-v2 P2.5 / D7 —— 把一份**已存在**的 artifact 晋升为知识库文件。

一个入口服务两条路（PRD §4.4 + §4.4.1）：
  · **自动索引**（D7）：用户上传的长期参照资料，零点击
  · **手动加入**：agent 产物与被判一次性的文件，用户显式确认

两条路走同一个函数，因为它们的底层动作完全相同：**复用同一个 blob**，只新建一行
`kb_files` 指向既有 `artifact_id`，再 enqueue 解析。区别只在"谁触发"与 scope。

不变量：
  · **零拷贝** —— 绝不 `put_bytes` 第二份字节（同一份文件在两个页面是同一个 blob）
  · **agent 产物永不自动** —— 自引用污染防线，`kind ∈ {deliverable, canvas_export}`
    只能走 `trigger="manual"`（守卫在 `promote_artifact_to_kb` 内，不靠调用方自觉）
  · **幂等** —— 已有活跃 kb_files 行时直接返回它，不建重复行
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact

log = logging.getLogger(__name__)

PromotionTrigger = Literal["auto", "manual"]

#: Agent-generated content **never** enters KB automatically (Verified Intelligence's
#: self-reference contamination defense). Require explicit user confirmation.
AGENT_PRODUCED_KINDS: frozenset[str] = frozenset({"deliverable", "canvas_export"})

#: Only files actively brought in by users may be auto-indexed.
AUTO_INDEXABLE_KINDS: frozenset[str] = frozenset({"user_upload"})

#: Artifact kinds explicitly promotable from Files. Runtime data must not become a trusted source,
#: even when its MIME can be parsed.
MANUAL_PROMOTABLE_KINDS: frozenset[str] = AGENT_PRODUCED_KINDS | AUTO_INDEXABLE_KINDS


class PromotionSkipped(Exception):
    """晋升未发生（不是错误，是规则）。`reason` 供日志/审计。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _kb_allowed_mimes() -> frozenset[str]:
    from bowhead_gateway.routes.kb import _ALLOWED_MIMES

    return frozenset(_ALLOWED_MIMES)


def is_kb_promotable(artifact: Artifact) -> bool:
    """Return whether Files may offer the explicit KB promotion action."""
    kind = getattr(artifact, "kind", None) or "tool_data"
    return kind in MANUAL_PROMOTABLE_KINDS and (artifact.mime or "") in _kb_allowed_mimes()


def should_auto_index(artifact: Artifact) -> tuple[bool, str]:
    """D7 判据：这份 artifact 该不该**自动**索引进知识库？

    返 `(是否索引, 理由)`；理由用于日志，让"为什么没自动进"可追。

    判据顺序刻意如此：kind（是不是用户带进来的）→ mime（技术上能不能解析）
    → retention_hint（语义上值不值得长期留存）。**mime 只做技术过滤，不做
    语义判断**——报价单 xlsx 与销售明细 xlsx 格式相同、命运不同（PRD §4.4.1-B）。
    """
    kind = getattr(artifact, "kind", None) or "tool_data"
    if kind in AGENT_PRODUCED_KINDS:
        return False, "agent_produced_needs_confirmation"
    if kind not in AUTO_INDEXABLE_KINDS:
        return False, f"kind_not_auto_indexable:{kind}"
    if (artifact.mime or "") not in _kb_allowed_mimes():
        return False, f"mime_not_parsable:{artifact.mime}"
    hint = getattr(artifact, "retention_hint", None)
    if hint != "long_term":
        # NULL (classification failed/not run) also lands here: fail-safe favors less indexing over incorrect indexing.
        return False, f"retention_hint:{hint or 'unset'}"
    return True, "long_term_user_upload"


async def promote_artifact_to_kb(
    *,
    artifact: Artifact,
    files_store: Any,
    scope: str = "personal",
    scope_id: str = "",
    workspace_id: str = "",
    team_id: str = "",
    user_id: str,
    trigger: PromotionTrigger,
    worker: Any | None = None,
) -> str:
    """建一行 kb_files 指向既有 artifact，并 enqueue 解析。返 kb file_id。

    幂等：已有活跃行时直接返回它（自动索引与用户手动加入可能撞同一份文件，
    PRD 开放问题 #6）。

    Raises:
        PromotionSkipped: 规则不允许（agent 产物走 auto、mime 不可解析等）。
    """
    kind = getattr(artifact, "kind", None) or "tool_data"
    # Guard here rather than in callers, automatically constraining every new call site.
    if trigger == "auto" and kind in AGENT_PRODUCED_KINDS:
        raise PromotionSkipped("agent_produced_cannot_auto_index")
    if (artifact.mime or "") not in _kb_allowed_mimes():
        raise PromotionSkipped(f"mime_not_parsable:{artifact.mime}")

    existing = await files_store.artifact_ids_in_kb([artifact.id])
    if artifact.id in existing:
        found = await files_store.find_active_by_artifact(artifact.id)
        if found:
            log.info("kb_promotion.idempotent aid=%s file_id=%s", artifact.id, found)
            return found

    file_id = await files_store.create(
        artifact_id=artifact.id,
        filename=artifact.filename,
        mime_type=artifact.mime,
        size_bytes=artifact.size,
        scope=scope,
        scope_id=scope_id,
        workspace_id=workspace_id,
        team_id=team_id,
        uploaded_by=user_id,
        use_llm_marker=False,
        doc_type_id=None,
    )
    log.info(
        "kb_promotion.created aid=%s file_id=%s trigger=%s scope=%s kind=%s",
        artifact.id,
        file_id,
        trigger,
        scope,
        kind,
    )
    if worker is not None:
        try:
            await worker.parse_and_index(file_id)
        except Exception as e:  # noqa: BLE001 - Parse failure must not roll back the created row.
            log.warning("kb_promotion.parse_failed file_id=%s error=%s", file_id, e)
    return file_id

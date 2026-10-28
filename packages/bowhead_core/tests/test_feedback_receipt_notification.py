"""回执最小化契约（批 2b-i，tech-design §2.4）。

回执会随会话分享/导出面外泄，所以断言的是**不含什么**：proposed 正文、
rubric、决策人身份都不得进 title/body/metadata。store 用桩捕获 upsert 参数。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bowhead_core.notification_service import NotificationService


class _CaptureStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def upsert_item_with_recipients(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return SimpleNamespace(id="ntf_1", **kwargs)


def test_receipt_contains_only_minimal_fields():
    store = _CaptureStore()
    service = NotificationService(store=store, user_store=SimpleNamespace())  # type: ignore[arg-type]
    item = asyncio.run(
        service.emit_feedback_candidate_receipt(
            team_id="t1",
            candidate_id="fbc_7f3a",
            target_kind="semantic",
            final_state="rejected",
            decided_at="2026-09-08T14:32:00Z",
            recipient_ids=["u_prop", "u_cosign"],
        )
    )
    assert item is not None
    call = store.calls[0]
    text = f"{call['title']} {call['body']}"
    assert "fbc_7f3a" in text and "业务口径" in text and "未采纳" in text
    assert "2026-09-08 14:32" in text and "14:32:00" not in text  # Minute precision; do not expose ISO microsecond strings to users.
    # No body/criteria/decision-maker: receipts and metadata contain only {id, type, terminal state, time}, allowing frontend localization.
    assert call["actor_user_id"] is None
    assert set(call["metadata"]) == {"candidateId", "finalState", "targetKind", "decidedAt"}
    assert call["recipients"] == [("u_prop", "feedback_contributor"), ("u_cosign", "feedback_contributor")]
    assert call["dedupe_key"] == "feedback.candidate.receipt:fbc_7f3a"


def test_receipt_without_recipients_is_a_noop():
    store = _CaptureStore()
    service = NotificationService(store=store, user_store=SimpleNamespace())  # type: ignore[arg-type]
    item = asyncio.run(
        service.emit_feedback_candidate_receipt(
            team_id="t1",
            candidate_id="fbc_x",
            target_kind="agent_def",
            final_state="accepted",
            decided_at="2026-09-08T14:32:00Z",
            recipient_ids=[],
        )
    )
    assert item is None and store.calls == []

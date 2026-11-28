"""Document matrix step 1 (S9): the usage event helpers themselves.

The routes are covered in the gateway's ``test_kb_matrix_usage.py``; these cases pin what no route
exercises yet: the ``chat`` origin reserved for step 2, an unknown origin or scope kind, and no log.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from bowhead_core.access_control import AccessContext
from bowhead_core.kb import matrix_usage

ACTOR = AccessContext(user_id="u_1", team_id="team_1", workspace_id="ws_1", workspace_role="member")


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, dict[str, Any]]] = []

    async def track(self, name: str, *, user_id: str | None = None, attrs: dict[str, Any] | None = None) -> None:
        self.events.append((name, user_id, dict(attrs or {})))


async def test_chat_origin_is_recorded_as_given() -> None:
    log = _Log()

    await matrix_usage.track_matrix_created(log, ACTOR, matrix_id="mx_1", origin="chat")

    assert log.events == [("kb.matrix.created", "u_1", {"team_id": "team_1", "workspace_id": "ws_1", "matrix_id": "mx_1", "origin": "chat"})]


async def test_unknown_origin_and_scope_kind_never_open_new_buckets(caplog: pytest.LogCaptureFixture) -> None:
    log = _Log()

    with caplog.at_level(logging.WARNING, logger="bowhead_core.kb.matrix_usage"):
        await matrix_usage.track_matrix_opened(log, ACTOR, matrix_id="mx_1", origin="feishu")  # type: ignore[arg-type]
    await matrix_usage.track_matrix_run_started(log, ACTOR, matrix_id="mx_1", scope_kind="every_cell_ever", cells=4, trial=False)

    assert [name for name, _user, _attrs in log.events] == ["kb.matrix.run_started"]
    assert log.events[0][2]["scope_kind"] == "other"
    assert any("kb_matrix.usage_rejected" in r.getMessage() for r in caplog.records)


async def test_no_log_or_nothing_verified_records_nothing() -> None:
    log = _Log()

    await matrix_usage.track_matrix_exported(None, ACTOR, matrix_id="mx_1")
    await matrix_usage.track_matrix_cells_verified(log, ACTOR, matrix_id="mx_1", count=0, bulk=True)

    assert log.events == []

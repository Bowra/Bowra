from typing import Any

import pytest
from bowhead_core.notification_store import ACTION_REQUIRED_TYPES, NotificationForUser
from bowhead_gateway.routes.notifications import (
    _to_dto,
)


def test_notification_dto_normalizes_legacy_scheduled_links() -> None:
    notification = NotificationForUser(
        id="n_1",
        team_id="team_1",
        category="async_job",
        type="scheduled.failed",
        priority="p1",
        title="Scheduled job failed",
        body=None,
        subject_type="scheduled_job",
        subject_id="job_1",
        actor_user_id=None,
        action_url="/settings/scheduled",
        actions=[{"id": "view", "label": "View", "kind": "link", "url": "/settings/scheduled"}],
        metadata={},
        dedupe_key="scheduled.failed:job_1",
        state="open",
        resolved_at=None,
        resolved_reason=None,
        created_at="2026-07-17T00:00:00+00:00",
        updated_at="2026-07-17T00:00:00+00:00",
        recipient_user_id="user_1",
        recipient_role="owner",
        delivered_at="2026-07-17T00:00:00+00:00",
        read_at=None,
        archived_at=None,
    )

    dto = _to_dto(notification)

    assert dto.actionUrl == "/team/settings/scheduled/"
    assert dto.actions[0].url == "/team/settings/scheduled/"


@pytest.mark.parametrize("type_", sorted(ACTION_REQUIRED_TYPES) + ["routine.delivered", "unknown.update"])
@pytest.mark.parametrize("state", ["open", "resolved"])
def test_notification_action_projection_uses_server_classification(type_: str, state: str) -> None:
    from dataclasses import fields

    # The stored dataclass contains heterogeneous scalar, collection and optional fields.
    values: dict[str, Any] = {field.name: None for field in fields(NotificationForUser)}
    values.update(
        id="n",
        team_id="team",
        category="test",
        type=type_,
        priority="p2",
        title="Title",
        actions=[],
        metadata={},
        dedupe_key="key",
        state=state,
        created_at="now",
        updated_at="now",
        recipient_user_id="user",
        delivered_at="now",
    )
    row = NotificationForUser(**values)
    assert _to_dto(row).actionRequired is (type_ in ACTION_REQUIRED_TYPES and state == "open")

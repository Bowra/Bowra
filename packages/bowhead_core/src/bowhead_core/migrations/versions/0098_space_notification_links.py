"""Repair stored Space assignment notification links without runtime redirects."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alembic import context

revision = "0098_space_notification_links"
down_revision = "0097_retire_billing_admin"
branch_labels = None
depends_on = None


def _repair_url(value: str | None) -> str | None:
    if not value:
        return value
    url = urlsplit(value)
    if url.scheme or url.netloc:
        return value
    if url.path.rstrip("/") == "/team/settings/workspaces":
        return urlunsplit(url._replace(path="/team/settings/spaces/"))
    if url.path.rstrip("/") == "/team/settings":
        query = parse_qsl(url.query, keep_blank_values=True)
        if ("tab", "workspaces") in query:
            return urlunsplit(
                url._replace(
                    path="/team/settings/spaces/",
                    query=urlencode([(key, val) for key, val in query if key != "tab"]),
                )
            )
    return value


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT space_notification_links")
    try:
        rows = connection.exec_driver_sql("SELECT id, action_url, actions_json FROM notification_items WHERE type='space_assignment_requested'")
        for notification_id, old_url, old_actions in rows:
            new_url = _repair_url(old_url)
            actions = json.loads(old_actions)
            changed_actions = False
            for action in actions:
                if action.get("kind") == "link":
                    before = action.get("url")
                    after = _repair_url(before)
                    if after != before:
                        action["url"] = after
                        changed_actions = True
            if new_url != old_url or changed_actions:
                connection.exec_driver_sql(
                    "UPDATE notification_items SET action_url=?, actions_json=? WHERE id=?",
                    (new_url, json.dumps(actions, ensure_ascii=False) if changed_actions else old_actions, notification_id),
                )
        connection.exec_driver_sql("RELEASE SAVEPOINT space_notification_links")
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT space_notification_links")
        connection.exec_driver_sql("RELEASE SAVEPOINT space_notification_links")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")

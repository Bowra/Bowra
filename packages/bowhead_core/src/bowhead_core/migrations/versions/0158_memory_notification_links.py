"""Repair stored memory suggestion notification links after the page moved to Settings."""

from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit

from alembic import context

revision = "0158_memory_notification_links"
down_revision = "0157_session_title_source"
branch_labels = None
depends_on = None


def _repair_url(value: str | None) -> str | None:
    if not value:
        return value
    url = urlsplit(value)
    if url.scheme or url.netloc or url.path not in {"/memory", "/memory/"}:
        return value
    return urlunsplit(url._replace(path="/settings/memory/"))


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT memory_notification_links")
    try:
        rows = connection.exec_driver_sql("SELECT id, action_url, actions_json FROM notification_items WHERE type='memory.candidate'")
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
        connection.exec_driver_sql("RELEASE SAVEPOINT memory_notification_links")
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT memory_notification_links")
        connection.exec_driver_sql("RELEASE SAVEPOINT memory_notification_links")
        raise


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")

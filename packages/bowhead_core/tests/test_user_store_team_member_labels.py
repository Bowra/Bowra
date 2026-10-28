"""UserStore.get_team_member_labels —— 知识库上传者等界面名称（知识库体验整治 PRD FR2.3）。

规则与团队目录一致：显示名优先，缺省用邮箱 @ 前部分；只解析该团队成员。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_core.user_store import UserStore

if TYPE_CHECKING:
    from pathlib import Path


async def test_labels_prefer_display_name_fall_back_to_email_local_and_stay_in_team(tmp_path: Path) -> None:
    users = UserStore(tmp_path / "bowhead.db")
    await users.initialize()
    team = await users.create_team(name="A", max_seats=4)
    other_team = await users.create_team(name="B", max_seats=4)
    named = await users.create_user(team_id=team.id, email="alice@example.com", display_name="Alice Zhang")
    unnamed = await users.create_user(team_id=team.id, email="exampleuser@example.com")
    outsider = await users.create_user(team_id=other_team.id, email="bob@example.com", display_name="Bob")

    labels = await users.get_team_member_labels(team.id, [named.id, unnamed.id, outsider.id, "usr_missing", ""])

    assert labels == {named.id: "Alice Zhang", unnamed.id: "exampleuser"}


async def test_labels_empty_inputs(tmp_path: Path) -> None:
    users = UserStore(tmp_path / "bowhead.db")
    await users.initialize()

    assert await users.get_team_member_labels("", ["usr_x"]) == {}
    assert await users.get_team_member_labels("team_x", []) == {}

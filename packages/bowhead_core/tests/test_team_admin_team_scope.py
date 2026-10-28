"""SP-AUTH-3 (概念术语审计 P0-2) — is_team_admin 必须按当前团队判.

无 team_id 的旧调用形态按 ``users.team_id``(主队) 判角色: 用户在主队 A 是
owner/team_admin、在当前活动队 B 只是 member 时, 会把 A 的管理员身份带进 B
(FEATURE_MULTI_TEAM_AUTHZ 下的跨团队提权). 授权体系.md §8.1: 禁止直接以
users.team_id 推断当前 Team.

本文件 = 行为 pin (team-scoped 判定) + 源码守卫 (禁止无 team_id 的调用复活).
"""

from __future__ import annotations

import re
from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore

_PKGS = Path(__file__).resolve().parents[2]


# ─────────────────────────────────────────────────────────────
# Behavior checks
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_team_admin_is_team_scoped(tmp_path: Path) -> None:
    """主队 owner + 别队普通 member: 别队判定必须是 False."""
    store = UserStore(tmp_path / "t.db")
    await store.initialize()

    team_a = await store.create_team(name="A", max_seats=5)
    team_b = await store.create_team(name="B", max_seats=5)
    u = await store.create_user(team_id=team_a.id, email="u@a.example", password="Passw0rd!")
    await store.set_team_creator(team_a.id, u.id)
    # u joins team B as an ordinary member (multiple team memberships).
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
            ("om_test_b_member", u.id, team_b.id, "member", "active", "2026-01-01T00:00:00Z"),
        )
        await db.commit()

    assert await store.is_team_admin(u.id, team_a.id) is True  # Team A owner
    assert await store.is_team_admin(u.id, team_b.id) is False  # Only a member in team B: no privilege escalation.
    assert await store.is_team_admin(u.id, "") is False  # Empty team → fail closed.


@pytest.mark.asyncio
async def test_transfer_team_owner_accepts_cross_main_team_member(tmp_path: Path) -> None:
    """SP-AUTH-6: 多团队下 owner 可转给「主队在别处」的本团队 active 成员."""
    store = UserStore(tmp_path / "t.db")
    await store.initialize()
    team_a = await store.create_team(name="A", max_seats=5)
    team_b = await store.create_team(name="B", max_seats=5)
    u = await store.create_user(team_id=team_a.id, email="u@a.example", password="Passw0rd!")
    await store.set_team_creator(team_a.id, u.id)
    v = await store.create_user(team_id=team_b.id, email="v@b.example", password="Passw0rd!")
    # v's primary team is B, but v joins A as an active member (multiple team memberships).
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,'member','active',?)",
            ("om_test_v_in_a", v.id, team_a.id, "2026-01-01T00:00:00Z"),
        )
        await db.commit()

    await store.transfer_team_owner(team_a.id, u.id, v.id)
    assert await store.get_team_role(v.id, team_a.id) == "owner"
    assert await store.get_team_role(u.id, team_a.id) == "team_admin"  # Demote the old owner to team_admin.


@pytest.mark.asyncio
async def test_is_team_admin_requires_team_id_argument(tmp_path: Path) -> None:
    """单参调用形态已删除 — 防止旧代码悄悄编译过."""
    store = UserStore(tmp_path / "t.db")
    await store.initialize()
    with pytest.raises(TypeError):
        store.is_team_admin("u_whoever")  # type: ignore[call-arg]


# ─────────────────────────────────────────────────────────────
# Source guard: prohibit single-argument .is_team_admin calls without team_id in src.
# ─────────────────────────────────────────────────────────────

_CALL_RE = re.compile(r"\.is_team_admin\(\s*([^()]*?)\s*\)")


def test_no_single_arg_is_team_admin_calls_in_src() -> None:
    offenders: list[str] = []
    for pkg in ("bowhead_core", "bowhead_gateway"):
        src = _PKGS / pkg / "src"
        for f in src.rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            for m in _CALL_RE.finditer(text):
                if "," not in m.group(1):
                    line = text[: m.start()].count("\n") + 1
                    offenders.append(f"{f.relative_to(_PKGS)}:{line} → is_team_admin({m.group(1)})")
    assert not offenders, f"is_team_admin 必须传当前团队 team_id (SP-AUTH-3): {offenders}"

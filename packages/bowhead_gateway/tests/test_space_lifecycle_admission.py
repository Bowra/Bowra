from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.user_store import UserStore
from bowhead_gateway.access_context import (
    AccessContextError,
    build_access_context,
    require_space_lifecycle_without_authorization,
)


@pytest.fixture
async def lifecycle_records(tmp_path):
    store = UserStore(tmp_path / "space-lifecycle.db")
    await store.initialize()
    team = await store.create_team(name="Lifecycle Team")
    member = await store.create_user(
        team_id=team.id,
        email="member@example.com",
        password="StrongPass123!",
    )
    await store.set_team_creator(team.id, member.id)
    workspace_id = await store.create_local_workspace(
        team_id=team.id,
        name="Lifecycle Space",
        creator_user_id=member.id,
    )
    return store, team.id, member.id, workspace_id


async def _build(
    store: UserStore,
    *,
    user_id: str,
    workspace_id: str,
    operation: SpaceOperation | None,
):
    kwargs = {
        "user_store": store,
        "access_store": None,
        "user_id": user_id,
        "workspace_id": workspace_id,
    }
    if operation is not None:
        kwargs["operation"] = operation
    return await build_access_context(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(SpaceOperation))
async def test_active_space_allows_every_operation(
    lifecycle_records,
    operation: SpaceOperation,
) -> None:
    store, team_id, user_id, workspace_id = lifecycle_records

    context = await _build(
        store,
        user_id=user_id,
        workspace_id=workspace_id,
        operation=operation,
    )

    assert context.team_id == team_id
    assert context.workspace_id == workspace_id


@pytest.mark.asyncio
async def test_archived_space_allows_original_member_history(
    lifecycle_records,
) -> None:
    store, _team_id, user_id, workspace_id = lifecycle_records
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (workspace_id,),
        )
        await db.commit()

    context = await _build(
        store,
        user_id=user_id,
        workspace_id=workspace_id,
        operation=SpaceOperation.READ_HISTORY,
    )

    assert context.workspace_id == workspace_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [SpaceOperation.WRITE, SpaceOperation.EXECUTE, None],
)
async def test_archived_space_rejects_new_work_and_unclassified_calls(
    lifecycle_records,
    operation: SpaceOperation | None,
) -> None:
    store, _team_id, user_id, workspace_id = lifecycle_records
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (workspace_id,),
        )
        await db.commit()

    with pytest.raises(AccessContextError) as exc_info:
        await _build(
            store,
            user_id=user_id,
            workspace_id=workspace_id,
            operation=operation,
        )

    assert exc_info.value.code == "workspace_archived"
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_removed_member_cannot_read_archived_history(
    lifecycle_records,
) -> None:
    store, _team_id, user_id, workspace_id = lifecycle_records
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (workspace_id,),
        )
        await db.execute(
            "UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?",
            (workspace_id, user_id),
        )
        await db.commit()

    with pytest.raises(AccessContextError) as exc_info:
        await _build(
            store,
            user_id=user_id,
            workspace_id=workspace_id,
            operation=SpaceOperation.READ_HISTORY,
        )

    assert exc_info.value.code == "ws_access_denied:not_a_member"
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_lifecycle_only_gate_does_not_check_membership(
    lifecycle_records,
) -> None:
    """弱闸的**契约**: 空间 active + 成员已被移出 ⇒ 它放行 —— 它根本不查成员资格.

    这不是期望的产品行为, 而是这个函数的定义。名字里的 ``without_authorization``
    说的就是这件事: 谁把它单独当授权用, 谁就拿到了这条放行
    (``space-removal-semantics.md`` §9 实测的三条读取面正是这么漏的)。
    """
    store, _team_id, user_id, workspace_id = lifecycle_records
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?",
            (workspace_id, user_id),
        )
        await db.commit()
    assert await store.get_member_role(workspace_id, user_id) is None

    # No raise: weak gate says nothing about removed members + active spaces.
    await require_space_lifecycle_without_authorization(
        user_store=store,
        workspace_id=workspace_id,
        operation=SpaceOperation.READ_HISTORY,
    )

    # Same input through the sole entry (strong gate) gives 403; difference entirely in membership checks.
    with pytest.raises(AccessContextError) as exc_info:
        await _build(
            store,
            user_id=user_id,
            workspace_id=workspace_id,
            operation=SpaceOperation.READ_HISTORY,
        )
    assert exc_info.value.code == "ws_access_denied:not_a_member"


@pytest.fixture
async def admin_override_records(tmp_path):
    """团队管理员 / 普通团队成员 / 别团队 owner —— 三个人都**不是**该空间的成员."""

    store = UserStore(tmp_path / "space-admin-override.db")
    await store.initialize()
    team = await store.create_team(name="Override Team", max_seats=10)
    owner = await store.create_user(
        team_id=team.id,
        email="owner@example.com",
        password="StrongPass123!",
    )
    await store.set_team_creator(team.id, owner.id)
    admin = await store.create_user(
        team_id=team.id,
        email="admin@example.com",
        password="StrongPass123!",
    )
    await store.promote_team_role(
        user_id=admin.id,
        team_id=team.id,
        new_role="team_admin",
        promoted_by=owner.id,
    )
    plain = await store.create_user(
        team_id=team.id,
        email="plain@example.com",
        password="StrongPass123!",
    )
    workspace_id = await store.create_local_workspace(
        team_id=team.id,
        name="Override Space",
        creator_user_id=owner.id,
    )

    other_team = await store.create_team(name="Other Team", max_seats=10)
    outsider = await store.create_user(
        team_id=other_team.id,
        email="outsider@example.com",
        password="StrongPass123!",
    )
    await store.set_team_creator(other_team.id, outsider.id)

    # Validate fixture: none of the three are space members; otherwise not asserting exemption below.
    for user in (admin, plain, outsider):
        assert await store.get_member_role(workspace_id, user.id) is None
    assert await store.get_team_role(admin.id, team.id) == "team_admin"

    return SimpleNamespace(
        store=store,
        team_id=team.id,
        workspace_id=workspace_id,
        admin=admin,
        plain=plain,
        outsider=outsider,
    )


async def _build_with_override(
    ctx: SimpleNamespace,
    *,
    user_id: str,
    allow_team_admin_override: bool,
):
    return await build_access_context(
        user_store=ctx.store,
        access_store=None,
        user_id=user_id,
        workspace_id=ctx.workspace_id,
        operation=SpaceOperation.READ_HISTORY,
        allow_team_admin_override=allow_team_admin_override,
    )


@pytest.mark.asyncio
async def test_team_admin_without_space_membership_denied_by_default(
    admin_override_records,
) -> None:
    """``allow_team_admin_override`` 默认 **False** —— 不传就没有豁免.

    钉住「豁免不是默认开的」: 它必须由调用方按路由语义显式要来 (PRD §5.5-A)。
    """
    ctx = admin_override_records

    with pytest.raises(AccessContextError) as exc_info:
        await _build_with_override(
            ctx,
            user_id=ctx.admin.id,
            allow_team_admin_override=False,
        )

    assert exc_info.value.code == "ws_access_denied:not_a_member"
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_team_admin_override_admits_non_member_admin(
    admin_override_records,
) -> None:
    """显式传 True: 本团队 team_admin 无需空间成员资格 —— 今天活着的那条通路."""
    ctx = admin_override_records

    context = await _build_with_override(
        ctx,
        user_id=ctx.admin.id,
        allow_team_admin_override=True,
    )

    assert context.workspace_id == ctx.workspace_id
    assert context.team_id == ctx.team_id
    assert context.is_team_admin is True
    # Exemption does not fabricate a space role: none exists; downstream must check is_team_admin, not workspace_role.
    assert context.workspace_role == ""


@pytest.mark.asyncio
async def test_team_admin_override_does_not_admit_plain_team_member(
    admin_override_records,
) -> None:
    """豁免只给 owner / team_admin —— 普通团队成员即使传 True 也仍然 403."""
    ctx = admin_override_records

    with pytest.raises(AccessContextError) as exc_info:
        await _build_with_override(
            ctx,
            user_id=ctx.plain.id,
            allow_team_admin_override=True,
        )

    assert exc_info.value.code == "ws_access_denied:not_a_member"
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_team_admin_override_never_crosses_team(
    admin_override_records,
) -> None:
    """别团队的 owner 传 True 也进不来: 豁免只覆盖 ``not_a_member``, 跨团队仍 fail-closed."""
    ctx = admin_override_records

    with pytest.raises(AccessContextError) as exc_info:
        await _build_with_override(
            ctx,
            user_id=ctx.outsider.id,
            allow_team_admin_override=True,
        )

    assert exc_info.value.code == "ws_access_denied:cross_team"
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_team_admin_override_still_applies_lifecycle_gate(
    admin_override_records,
) -> None:
    """豁免绕过的是成员校验, **不是**生命周期闸: 归档空间里写操作照样 409."""
    ctx = admin_override_records
    async with aiosqlite.connect(ctx.store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (ctx.workspace_id,),
        )
        await db.commit()

    with pytest.raises(AccessContextError) as exc_info:
        await build_access_context(
            user_store=ctx.store,
            access_store=None,
            user_id=ctx.admin.id,
            workspace_id=ctx.workspace_id,
            operation=SpaceOperation.WRITE,
            allow_team_admin_override=True,
        )

    assert exc_info.value.code == "workspace_archived"
    assert exc_info.value.status_code == 409

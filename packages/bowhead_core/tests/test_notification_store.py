from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.approval_gate import ApprovalGate
from bowhead_core.approval_store import ApprovalStore
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore
from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec, build_default_registry
from bowhead_core.user_store import UserStore
from bowhead_router.types import ToolSchema

from bowhead_core import approval_gate as approval_gate_mod

if TYPE_CHECKING:
    from pathlib import Path


async def _unreachable_handler(_args, _ctx):
    """闸应在 handler 之前短路——真被调到说明拦截失效。"""
    raise AssertionError("approval gate should short-circuit before the handler runs")


class _Bus:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


class _StalePendingApprovalStore:
    def __init__(self, stale, latest) -> None:
        self.stale = stale
        self.latest = latest

    async def list_pending(self, *, limit: int = 500):
        return [self.stale]

    async def get(self, approval_id: str):
        if approval_id == self.stale.id:
            return self.latest
        return None


@pytest.mark.asyncio
async def test_initialize_renames_legacy_notification_org_id(tmp_path: Path) -> None:
    db_path = tmp_path / "notifications.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE notification_items (
                id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL,
                category TEXT NOT NULL,
                type TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'p2',
                title TEXT NOT NULL,
                body TEXT,
                subject_type TEXT,
                subject_id TEXT,
                actor_user_id TEXT,
                action_url TEXT,
                actions_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                dedupe_key TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'open',
                resolved_at TEXT,
                resolved_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(org_id, dedupe_key)
            );
            """
        )
        await db.commit()

    store = NotificationStore(str(db_path))
    await store.initialize()

    async with aiosqlite.connect(db_path) as db:
        columns = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(notification_items)")}
    assert "team_id" in columns
    assert "org_id" not in columns


@pytest.mark.asyncio
async def test_notification_counts_keep_action_required_until_resolved(tmp_path: Path) -> None:
    store = NotificationStore(str(tmp_path / "notifications.db"))
    await store.initialize()
    item = await store.upsert_item(
        team_id="team_1",
        category="approval",
        type="approval.required",
        priority="p1",
        title="Approval required",
        body=None,
        subject_type="approval",
        subject_id="apr_1",
        actor_user_id="user_1",
        action_url="/admin/approvals?approvalId=apr_1",
        actions=[],
        metadata={},
        dedupe_key="approval.required:apr_1",
    )
    await store.add_recipients(item.id, [("admin_1", "approver_admin")])

    counts = await store.notification_count(user_id="admin_1")
    assert counts.action_required == 1
    assert counts.updates_unread == 0
    assert counts.total_badge == 1

    assert await store.mark_read(notification_id=item.id, user_id="admin_1") is True
    counts = await store.notification_count(user_id="admin_1")
    assert counts.action_required == 1

    resolved = await store.resolve_by_dedupe(
        team_id="team_1",
        dedupe_key="approval.required:apr_1",
        reason="approved",
    )
    assert resolved is not None
    already_resolved = await store.resolve_by_dedupe(
        team_id="team_1",
        dedupe_key="approval.required:apr_1",
        reason="approved",
    )
    assert already_resolved is None
    counts = await store.notification_count(user_id="admin_1")
    assert counts.action_required == 0
    assert counts.total_badge == 0


@pytest.mark.asyncio
async def test_archive_removes_notification_from_lists_and_counts(tmp_path: Path) -> None:
    store = NotificationStore(str(tmp_path / "notifications.db"))
    await store.initialize()
    action = await store.upsert_item(
        team_id="team_1",
        category="approval",
        type="approval.required",
        priority="p1",
        title="Approval required",
        body=None,
        subject_type="approval",
        subject_id="apr_1",
        actor_user_id="user_1",
        action_url="/admin/approvals?approvalId=apr_1",
        actions=[],
        metadata={},
        dedupe_key="approval.required:apr_1",
    )
    update = await store.upsert_item(
        team_id="team_1",
        category="approval",
        type="approval.rejected",
        priority="p2",
        title="Approval rejected",
        body=None,
        subject_type="approval",
        subject_id="apr_1",
        actor_user_id="admin_1",
        action_url="/?session=s1",
        actions=[],
        metadata={},
        dedupe_key="approval.rejected:apr_1",
    )
    await store.add_recipients(action.id, [("admin_1", "approver_admin")])
    await store.add_recipients(update.id, [("admin_1", "requester")])

    counts = await store.notification_count(user_id="admin_1")
    assert counts.action_required == 1
    assert counts.updates_unread == 1

    assert await store.archive(notification_id=action.id, user_id="admin_1") is True
    counts = await store.notification_count(user_id="admin_1")
    assert counts.action_required == 0
    assert counts.updates_unread == 1
    items, _ = await store.list_for_user(user_id="admin_1", tab="all")
    assert [item.id for item in items] == [update.id]

    assert await store.archive(notification_id=update.id, user_id="admin_1") is True
    counts = await store.notification_count(user_id="admin_1")
    assert counts.action_required == 0
    assert counts.updates_unread == 0
    items, _ = await store.list_for_user(user_id="admin_1", tab="all")
    assert items == []


@pytest.mark.asyncio
async def test_prune_archived_deletes_only_expired_archived_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "notifications.db"
    store = NotificationStore(str(db_path))
    await store.initialize()
    old = await store.upsert_item(
        team_id="team_1",
        category="approval",
        type="approval.rejected",
        priority="p2",
        title="Old archived",
        body=None,
        subject_type="approval",
        subject_id="apr_old",
        actor_user_id="admin_1",
        action_url="/?session=s1",
        actions=[],
        metadata={},
        dedupe_key="approval.rejected:apr_old",
    )
    recent = await store.upsert_item(
        team_id="team_1",
        category="approval",
        type="approval.rejected",
        priority="p2",
        title="Recent archived",
        body=None,
        subject_type="approval",
        subject_id="apr_recent",
        actor_user_id="admin_1",
        action_url="/?session=s1",
        actions=[],
        metadata={},
        dedupe_key="approval.rejected:apr_recent",
    )
    active = await store.upsert_item(
        team_id="team_1",
        category="approval",
        type="approval.required",
        priority="p1",
        title="Active action",
        body=None,
        subject_type="approval",
        subject_id="apr_active",
        actor_user_id="user_1",
        action_url="/admin/approvals?approvalId=apr_active",
        actions=[],
        metadata={},
        dedupe_key="approval.required:apr_active",
    )
    await store.add_recipients(old.id, [("user_old", "requester")])
    await store.add_recipients(recent.id, [("user_recent", "requester")])
    await store.add_recipients(active.id, [("admin_1", "approver_admin")])
    assert await store.archive(notification_id=old.id, user_id="user_old") is True
    assert await store.archive(notification_id=recent.id, user_id="user_recent") is True

    now = datetime(2026, 6, 10, tzinfo=UTC)
    old_archived_at = (now - timedelta(days=200)).isoformat(timespec="seconds")
    cutoff = (now - timedelta(days=180)).isoformat(timespec="seconds")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE notification_items SET created_at=?, updated_at=? WHERE id=?",
            (old_archived_at, old_archived_at, old.id),
        )
        await db.execute(
            "UPDATE notification_recipients SET archived_at=? WHERE notification_id=?",
            (old_archived_at, old.id),
        )
        await db.commit()

    counts = await store.prune_archived(older_than=cutoff, limit=100)

    assert counts == {"recipients_deleted": 1, "items_deleted": 1}
    async with (
        aiosqlite.connect(db_path) as db,
        db.execute("SELECT id FROM notification_items ORDER BY id") as cur,
    ):
        ids = {row[0] for row in await cur.fetchall()}
    assert old.id not in ids
    assert recent.id in ids
    assert active.id in ids
    counts = await store.notification_count(user_id="admin_1")
    assert counts.action_required == 1


@pytest.mark.asyncio
async def test_prune_archived_keeps_recent_orphan_items(tmp_path: Path) -> None:
    db_path = tmp_path / "notifications.db"
    store = NotificationStore(str(db_path))
    await store.initialize()
    item = await store.upsert_item(
        team_id="team_1",
        category="approval",
        type="approval.rejected",
        priority="p2",
        title="Fresh orphan",
        body=None,
        subject_type="approval",
        subject_id="apr_fresh",
        actor_user_id="admin_1",
        action_url="/?session=s1",
        actions=[],
        metadata={},
        dedupe_key="approval.rejected:apr_fresh",
    )

    now = datetime(2026, 6, 10, tzinfo=UTC)
    cutoff = (now - timedelta(days=180)).isoformat(timespec="seconds")
    counts = await store.prune_archived(older_than=cutoff, limit=100)

    assert counts == {"recipients_deleted": 0, "items_deleted": 0}
    async with (
        aiosqlite.connect(db_path) as db,
        db.execute(
            "SELECT COUNT(*) FROM notification_items WHERE id=?",
            (item.id,),
        ) as cur,
    ):
        assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_approval_gate_is_idempotent_and_emits_admin_notification(tmp_path: Path, monkeypatch) -> None:
    """gate → 入队 → 管理员通知 → approval.required emit，且同 tool_call_id 幂等。

    2026-07-21：原来借真实的 bi_update_measures 当被测工具；4 个 BI 写工具已整族删除
    （datarc 无写 API），`SUPPORTED_NATIVE_WRITE_TOOLS` 随之清空。这里注册一个合成的
    needs_approval 工具并把它放进那个集合——被测的是闸本身的行为，与具体工具无关。
    """
    db = tmp_path / "approval_gate.db"
    users = UserStore(db)
    await users.initialize()
    approval_store = ApprovalStore(str(db))
    await approval_store.initialize()
    notification_store = NotificationStore(str(db))
    await notification_store.initialize()

    team = await users.create_team(name="Acme", max_seats=10)
    admin = await users.create_user(team_id=team.id, email="admin@example.com", password="Passw0rd!")
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    await users.set_team_creator(team.id, admin.id)
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=owner.id,
    )

    gate = ApprovalGate(
        approval_store=approval_store,
        notification_service=NotificationService(notification_store, users),
    )
    ctx = ToolExecutionContext(
        workspace_id=workspace_id,
        member_uid="member_1",
        user_id=owner.id,
        turn_id="turn_1",
        session_id="session_1",
        datarc=object(),
    )
    registry = build_default_registry()
    probe = "approval_gate_probe"
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name=probe,
                description="approval gate probe (write-like)",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_unreachable_handler,
            needs_approval=True,
            risk_level="high",
        ),
    )
    monkeypatch.setattr(approval_gate_mod, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset({probe}))
    args = {"payload": {"name": "Revenue"}, "reason": "create KPI"}
    bus = _Bus()

    first = await gate.maybe_gate(
        registry=registry,
        tool_name=probe,
        arguments=args,
        ctx=ctx,
        tool_call_id="call_1",
        bus=bus,
    )
    second = await gate.maybe_gate(
        registry=registry,
        tool_name=probe,
        arguments=args,
        ctx=ctx,
        tool_call_id="call_1",
    )

    assert first is not None and first.error is None
    assert second is not None and second.error is None
    assert first.arguments == {}
    assert first.result["approval_id"] == second.result["approval_id"]
    pending = await approval_store.list_pending()
    assert len(pending) == 1
    assert pending[0].arguments == args

    counts = await notification_store.notification_count(user_id=admin.id)
    assert counts.action_required == 1
    assert len(bus.events) == 1
    assert bus.events[0].event_type == "approval.required"
    assert bus.events[0].payload == {
        "card_id": "turn_1",
        "approval_id": first.result["approval_id"],
        "tool_name": probe,
        "risk_level": "high",
        "created": True,
    }


@pytest.mark.asyncio
async def test_pending_approval_backfill_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "backfill.db"
    users = UserStore(db)
    await users.initialize()
    approval_store = ApprovalStore(str(db))
    await approval_store.initialize()
    notification_store = NotificationStore(str(db))
    await notification_store.initialize()

    team = await users.create_team(name="Acme", max_seats=10)
    admin = await users.create_user(team_id=team.id, email="admin@example.com", password="Passw0rd!")
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    await users.set_team_creator(team.id, admin.id)
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=owner.id,
    )
    approval = await approval_store.enqueue(
        turn_id="turn_backfill",
        session_id="session_backfill",
        workspace_id=workspace_id,
        member_uid="member_1",
        user_id=owner.id,
        tool_name="bi_update_measures",
        arguments={"payload": {"name": "Revenue"}},
        reason="rename",
        risk_level="high",
    )

    service = NotificationService(notification_store, users)
    await service.backfill_pending_approvals(approval_store)
    await service.backfill_pending_approvals(approval_store)

    items, _ = await notification_store.list_for_user(user_id=admin.id, tab="action")
    assert len(items) == 1
    assert items[0].subject_id == approval.id
    counts = await notification_store.notification_count(user_id=admin.id)
    assert counts.action_required == 1


@pytest.mark.asyncio
async def test_backfill_rechecks_status_before_phantom_insert_without_existing_row(
    tmp_path: Path,
) -> None:
    db = tmp_path / "phantom_backfill.db"
    users = UserStore(db)
    await users.initialize()
    approval_store = ApprovalStore(str(db))
    await approval_store.initialize()
    notification_store = NotificationStore(str(db))
    await notification_store.initialize()

    team = await users.create_team(name="Acme", max_seats=10)
    admin = await users.create_user(team_id=team.id, email="admin@example.com", password="Passw0rd!")
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    await users.set_team_creator(team.id, admin.id)
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=owner.id,
    )
    stale_pending = await approval_store.enqueue(
        turn_id="turn_phantom",
        session_id="session_phantom",
        workspace_id=workspace_id,
        member_uid="member_1",
        user_id=owner.id,
        tool_name="bi_update_measures",
        arguments={"payload": {"name": "Revenue"}},
        reason="rename",
        risk_level="high",
    )
    latest, transitioned = await approval_store.set_decision_once(
        approval_id=stale_pending.id,
        status="rejected",
        decided_by=admin.id,
    )
    assert transitioned is True
    assert latest is not None

    service = NotificationService(notification_store, users)
    emitted = await service.backfill_pending_approvals(
        _StalePendingApprovalStore(stale_pending, latest),
    )

    assert emitted == 0
    counts = await notification_store.notification_count(user_id=admin.id)
    assert counts.action_required == 0
    items, _ = await notification_store.list_for_user(user_id=admin.id, tab="action")
    assert items == []


@pytest.mark.asyncio
async def test_stale_approval_required_emit_does_not_reopen_resolved_action(
    tmp_path: Path,
) -> None:
    db = tmp_path / "stale_emit.db"
    users = UserStore(db)
    await users.initialize()
    approval_store = ApprovalStore(str(db))
    await approval_store.initialize()
    notification_store = NotificationStore(str(db))
    await notification_store.initialize()

    team = await users.create_team(name="Acme", max_seats=10)
    admin = await users.create_user(team_id=team.id, email="admin@example.com", password="Passw0rd!")
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    await users.set_team_creator(team.id, admin.id)
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=owner.id,
    )
    approval = await approval_store.enqueue(
        turn_id="turn_stale",
        session_id="session_stale",
        workspace_id=workspace_id,
        member_uid="member_1",
        user_id=owner.id,
        tool_name="bi_update_measures",
        arguments={"payload": {"name": "Revenue"}},
        reason="rename",
        risk_level="high",
    )

    service = NotificationService(notification_store, users)
    assert await service.emit_approval_required(approval) is not None
    decided, transitioned = await approval_store.set_decision_once(
        approval_id=approval.id,
        status="rejected",
        decided_by=admin.id,
    )
    assert transitioned is True
    assert decided is not None
    await service.resolve_approval_required(decided, reason="rejected")
    counts = await notification_store.notification_count(user_id=admin.id)
    assert counts.action_required == 0

    assert await service.emit_approval_required(approval) is None
    latest = await approval_store.get(approval.id)
    assert latest is not None
    emitted = await service.backfill_pending_approvals(
        _StalePendingApprovalStore(approval, latest),
    )

    assert emitted == 0
    counts = await notification_store.notification_count(user_id=admin.id)
    assert counts.action_required == 0
    items, _ = await notification_store.list_for_user(user_id=admin.id, tab="action")
    assert items == []


@pytest.mark.asyncio
async def test_reconcile_resolves_orphaned_required_actions(tmp_path: Path) -> None:
    db = tmp_path / "reconcile.db"
    users = UserStore(db)
    await users.initialize()
    approval_store = ApprovalStore(str(db))
    await approval_store.initialize()
    notification_store = NotificationStore(str(db))
    await notification_store.initialize()

    team = await users.create_team(name="Acme", max_seats=10)
    admin = await users.create_user(team_id=team.id, email="admin@example.com", password="Passw0rd!")
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    await users.set_team_creator(team.id, admin.id)
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=owner.id,
    )
    approval = await approval_store.enqueue(
        turn_id="turn_reconcile",
        session_id="session_reconcile",
        workspace_id=workspace_id,
        member_uid="member_1",
        user_id=owner.id,
        tool_name="bi_update_measures",
        arguments={"payload": {"name": "Revenue"}},
        reason="rename",
        risk_level="high",
    )

    service = NotificationService(notification_store, users)
    assert await service.emit_approval_required(approval) is not None
    counts = await notification_store.notification_count(user_id=admin.id)
    assert counts.action_required == 1
    decided, transitioned = await approval_store.set_decision_once(
        approval_id=approval.id,
        status="rejected",
        decided_by=admin.id,
    )
    assert transitioned is True
    assert decided is not None

    resolved = await service.reconcile_approval_required(approval_store)

    assert resolved == 1
    counts = await notification_store.notification_count(user_id=admin.id)
    assert counts.action_required == 0


# ─── Frozen notification time (2026-09-14 116 observation: today's learned memory suggestion showed "53天前" on Home) ─────


async def _upsert(store, *, dedupe_key: str, title: str, type_: str = "memory.candidate", **kw):
    return await store.upsert_item(
        team_id="team_1",
        category="memory",
        type=type_,
        priority="p2",
        title=title,
        body=title,
        subject_type="memory_candidates",
        subject_id="u1",
        actor_user_id=None,
        action_url="/memory",
        actions=[],
        metadata={},
        dedupe_key=dedupe_key,
        **kw,
    )


def _clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    import bowhead_core.notification_store as module

    clock = {"t": "2026-07-23T07:15:42+00:00"}
    monkeypatch.setattr(module, "_now", lambda: clock["t"])
    return clock


async def test_a_refreshed_aggregate_notification_carries_its_new_time_and_rises_to_the_top(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ `memory.candidate` 按用户一条 dedupe_key，每批新建议 upsert 同一行：正文换新，
    `created_at` 却停在第一批那天 ⇒ 首页 / 铃铛都显示「53天前」，列表按 `created_at` 排序还把它压在底下。

    ★ 突变验证：SQL 里去掉 `created_at=CASE ...` 那一段 ⇒ 本用例红。
    """
    clock = _clock(monkeypatch)
    store = NotificationStore(str(tmp_path / "n.db"))
    await store.initialize()

    first = await _upsert(store, dedupe_key="memory.candidate:u1", title="第一批", refresh_created_at=True)
    await store.add_recipients(first.id, [("u1", "owner")])
    clock["t"] = "2026-09-01T00:00:00+00:00"
    other = await _upsert(store, dedupe_key="share:doc_1", title="别的通知", type_="content_shared")
    await store.add_recipients(other.id, [("u1", "owner")])
    clock["t"] = "2026-09-14T06:10:39+00:00"
    again = await _upsert(store, dedupe_key="memory.candidate:u1", title="今天这批", refresh_created_at=True)

    assert again.id == first.id
    assert again.created_at == "2026-09-14T06:10:39+00:00"
    rows, _cursor = await store.list_for_user(user_id="u1", tab="all", limit=10)
    assert [r.id for r in rows][:2] == [first.id, other.id], "刚刷新的那条必须排在前面，不能按第一批的日子沉底"


async def test_an_open_streak_keeps_its_first_time_unless_the_caller_opts_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """负对照：`scheduled.failed` 按 job 一条是**有意**把连续失败折成一条（`emit_scheduled_failed` docstring），
    未恢复期间显示首次失败时间是对的。不许顺手把所有 upsert 都刷新掉——那还会打乱别的排序。"""
    clock = _clock(monkeypatch)
    store = NotificationStore(str(tmp_path / "n.db"))
    await store.initialize()

    first = await _upsert(store, dedupe_key="scheduled.failed:job_1", title="失败", type_="scheduled.failed")
    clock["t"] = "2026-08-23T00:00:00+00:00"
    again = await _upsert(store, dedupe_key="scheduled.failed:job_1", title="又失败", type_="scheduled.failed")

    assert again.id == first.id
    assert again.created_at == "2026-07-23T07:15:42+00:00"


async def test_reopening_a_resolved_notification_is_a_new_occurrence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已恢复又再失败＝新的一次。此前重开时 `created_at` 停在上一段的日子（116 上 `scheduled.failed` 冻结 30 天）。

    ★ 突变验证：CASE 里去掉 `(? AND notification_items.state != 'open')` ⇒ 本用例红。
    也钉住 SQLite「SET 右侧读旧行」：若读到的是同一语句刚 SET 的 'open'，重开永远判不出来。
    """
    clock = _clock(monkeypatch)
    store = NotificationStore(str(tmp_path / "n.db"))
    await store.initialize()

    first = await _upsert(store, dedupe_key="scheduled.failed:job_1", title="失败", type_="scheduled.failed")
    await store.resolve_by_dedupe(team_id="team_1", dedupe_key="scheduled.failed:job_1", reason="recovered")
    clock["t"] = "2026-08-23T00:00:00+00:00"
    reopened = await _upsert(store, dedupe_key="scheduled.failed:job_1", title="又失败", type_="scheduled.failed")
    assert reopened.id == first.id
    assert reopened.created_at == "2026-08-23T00:00:00+00:00"

    # Negative control: caller explicitly declines reopening; row stays resolved and timestamp unchanged.
    clock["t"] = "2026-07-23T07:15:42+00:00"
    kept = await _upsert(store, dedupe_key="scheduled.paused:job_2", title="暂停", type_="scheduled.paused")
    await store.resolve_by_dedupe(team_id="team_1", dedupe_key="scheduled.paused:job_2", reason="recovered")
    clock["t"] = "2026-08-23T00:00:00+00:00"
    still = await _upsert(store, dedupe_key="scheduled.paused:job_2", title="暂停", type_="scheduled.paused", reopen_resolved=False)
    assert still.id == kept.id
    assert still.created_at == "2026-07-23T07:15:42+00:00"


async def test_a_new_occurrence_makes_the_row_unread_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ `memory.candidate` 每批新建议复用同一行。用户读过上一批之后，新一批进来
    那行仍然是已读（前端 `unread = !item.readAt`）——新内容看起来像他已经处理过的。
    `upsert_item_with_recipients` 的重开分支本来就会 `read_at=NULL`，这条路漏了。

    ⚠ 角标数字不是这个问题：action 类计数只看 `state='open'`、不看已读。坏的是行上的未读状态。
    ★ 突变验证：删掉 upsert_item 里那段 `read_at=NULL` ⇒ 本用例红。
    """
    clock = _clock(monkeypatch)
    store = NotificationStore(str(tmp_path / "n.db"))
    await store.initialize()

    first = await _upsert(store, dedupe_key="memory.candidate:u1", title="第一批", refresh_created_at=True)
    await store.add_recipients(first.id, [("u1", "owner")])
    assert await store.mark_read(notification_id=first.id, user_id="u1") is True
    rows, _c = await store.list_for_user(user_id="u1", tab="all", limit=10)
    assert rows[0].read_at is not None, "前置：用户确实读过了"

    clock["t"] = "2026-09-14T06:10:39+00:00"
    await _upsert(store, dedupe_key="memory.candidate:u1", title="今天这批", refresh_created_at=True)

    rows, _c = await store.list_for_user(user_id="u1", tab="all", limit=10)
    assert rows[0].read_at is None, "新一批是新的一次，那行必须重新显示成未读"


async def test_a_continuing_streak_does_not_resurrect_the_unread_dot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """负对照：`scheduled.failed` 按 job 折成一条，同一段失败再报一次**不是**新的一次
    （`created_at` 也不刷新）⇒ 用户看过就该保持已读，不然每次重试都把它顶成未读。"""
    clock = _clock(monkeypatch)
    store = NotificationStore(str(tmp_path / "n.db"))
    await store.initialize()

    first = await _upsert(store, dedupe_key="scheduled.failed:job_1", title="失败", type_="scheduled.failed")
    await store.add_recipients(first.id, [("u1", "owner")])
    await store.mark_read(notification_id=first.id, user_id="u1")

    clock["t"] = "2026-08-23T00:00:00+00:00"
    await _upsert(store, dedupe_key="scheduled.failed:job_1", title="又失败", type_="scheduled.failed")

    rows, _c = await store.list_for_user(user_id="u1", tab="all", limit=10)
    assert rows[0].read_at is not None, "同一段失败的再次上报不该把已读顶回未读"

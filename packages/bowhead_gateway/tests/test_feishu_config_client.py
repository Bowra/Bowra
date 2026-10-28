from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_gateway.feishu.client import FeishuClient
from bowhead_gateway.feishu.config import DbBackedFeishuAppConfigResolver
from bowhead_gateway.feishu.store import FeishuEventStore
from bowhead_gateway.settings import Settings


def test_feishu_store_renames_legacy_team_columns_before_indexes(tmp_path) -> None:
    db_path = tmp_path / "feishu.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE feishu_setup_sessions (
                id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL,
                workspace_id TEXT,
                created_by_user_id TEXT NOT NULL,
                state TEXT NOT NULL,
                nonce TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                tenant_key TEXT,
                app_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE feishu_app_configs (
                id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL,
                tenant_key TEXT NOT NULL DEFAULT '',
                app_type TEXT NOT NULL DEFAULT 'internal',
                enabled INTEGER NOT NULL DEFAULT 0,
                event_ingress TEXT NOT NULL DEFAULT 'webhook',
                app_id TEXT NOT NULL DEFAULT '',
                app_secret_ref TEXT,
                verification_token_ref TEXT,
                encrypt_key_ref TEXT,
                bot_open_id TEXT NOT NULL DEFAULT '',
                group_public_answers_enabled INTEGER NOT NULL DEFAULT 0,
                created_by_user_id TEXT NOT NULL,
                updated_by_user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    asyncio.run(FeishuEventStore(db_path).initialize())

    with sqlite3.connect(db_path) as db:
        for table in ("feishu_setup_sessions", "feishu_app_configs"):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            assert "team_id" in columns
            assert "org_id" not in columns


def test_db_backed_feishu_config_resolver_caches_until_invalidated(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    asyncio.run(_write_app_config(store, app_id="cli_db_one"))
    resolver = DbBackedFeishuAppConfigResolver(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_env",
            feishu_app_secret="env_secret",
        ),
        str(store.db_path),
    )

    assert resolver.resolve("").app_id == "cli_db_one"

    asyncio.run(_write_app_config(store, app_id="cli_db_two"))

    assert resolver.resolve("").app_id == "cli_db_one"

    resolver.invalidate("")

    assert resolver.resolve("").app_id == "cli_db_two"


def test_db_backed_feishu_config_resolver_invalidating_default_row_clears_tenant_fallback_cache(
    tmp_path,
) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    asyncio.run(store.initialize())
    asyncio.run(_write_app_config(store, app_id="cli_db_one"))
    resolver = DbBackedFeishuAppConfigResolver(
        Settings(
            feishu_enabled=True,
            feishu_app_id="cli_env",
            feishu_app_secret="env_secret",
        ),
        str(store.db_path),
    )

    assert resolver.resolve("tenant_a").app_id == "cli_db_one"

    asyncio.run(_write_app_config(store, app_id="cli_db_two"))
    resolver.invalidate("")

    assert resolver.resolve("tenant_a").app_id == "cli_db_two"


def test_feishu_client_retries_idempotent_patch_request() -> None:
    attempts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request.url.path)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx2.Response(
                200,
                request=request,
                json={"code": 0, "data": {"tenant_access_token": "tok_1", "expire": 7200}},
            )
        if request.url.path == "/open-apis/im/v1/messages/om_card":
            if attempts.count(request.url.path) == 1:
                return httpx2.Response(502, request=request, text="bad gateway")
            return httpx2.Response(200, request=request, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = FeishuClient(
        DbBackedFeishuAppConfigResolver(
            Settings(feishu_enabled=True, feishu_app_id="cli_app", feishu_app_secret="cli_secret"),
            ":memory:",
        ),
        transport=httpx2.MockTransport(handler),
        retry_backoff_base=0.0,
    )

    async def _run() -> None:
        try:
            await client.patch_card(message_id="om_card", card={"config": {"update_multi": True}})
        finally:
            await client.aclose()

    asyncio.run(_run())

    assert attempts.count("/open-apis/auth/v3/tenant_access_token/internal") == 1
    assert attempts.count("/open-apis/im/v1/messages/om_card") == 2


def test_feishu_client_does_not_retry_non_idempotent_reply_without_uuid() -> None:
    attempts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request.url.path)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx2.Response(
                200,
                request=request,
                json={"code": 0, "data": {"tenant_access_token": "tok_1", "expire": 7200}},
            )
        if request.url.path == "/open-apis/im/v1/messages/om_reply/reply":
            return httpx2.Response(502, request=request, text="bad gateway")
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = FeishuClient(
        DbBackedFeishuAppConfigResolver(
            Settings(feishu_enabled=True, feishu_app_id="cli_app", feishu_app_secret="cli_secret"),
            ":memory:",
        ),
        transport=httpx2.MockTransport(handler),
        retry_backoff_base=0.0,
    )

    async def _run() -> None:
        with pytest.raises(httpx2.HTTPStatusError):
            try:
                await client.reply_message(
                    message_id="om_reply",
                    msg_type="interactive",
                    content={"elements": []},
                )
            finally:
                await client.aclose()

    asyncio.run(_run())

    assert attempts.count("/open-apis/auth/v3/tenant_access_token/internal") == 1
    assert attempts.count("/open-apis/im/v1/messages/om_reply/reply") == 1


def test_feishu_client_retries_reply_when_uuid_makes_it_idempotent() -> None:
    attempts: list[str] = []
    reply_payloads: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request.url.path)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx2.Response(
                200,
                request=request,
                json={"code": 0, "data": {"tenant_access_token": "tok_1", "expire": 7200}},
            )
        if request.url.path == "/open-apis/im/v1/messages/om_reply/reply":
            reply_payloads.append(json.loads(request.content.decode("utf-8")))
            if attempts.count(request.url.path) == 1:
                return httpx2.Response(502, request=request, text="bad gateway")
            return httpx2.Response(200, request=request, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = FeishuClient(
        DbBackedFeishuAppConfigResolver(
            Settings(feishu_enabled=True, feishu_app_id="cli_app", feishu_app_secret="cli_secret"),
            ":memory:",
        ),
        transport=httpx2.MockTransport(handler),
        retry_backoff_base=0.0,
    )

    async def _run() -> None:
        try:
            await client.reply_message(
                message_id="om_reply",
                msg_type="interactive",
                content={"elements": []},
                uuid="retry_safe_uuid",
            )
        finally:
            await client.aclose()

    asyncio.run(_run())

    assert attempts.count("/open-apis/auth/v3/tenant_access_token/internal") == 1
    assert attempts.count("/open-apis/im/v1/messages/om_reply/reply") == 2
    assert reply_payloads == [
        {
            "msg_type": "interactive",
            "content": '{"elements":[]}',
            "uuid": "retry_safe_uuid",
        },
        {
            "msg_type": "interactive",
            "content": '{"elements":[]}',
            "uuid": "retry_safe_uuid",
        },
    ]


@pytest.mark.asyncio
async def test_feishu_event_store_hygiene_sweep_expires_and_prunes_old_rows(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    await store.initialize()

    binding_session = await store.create_binding_session(
        tenant_key="tenant_1",
        open_id="ou_1",
        union_id=None,
        email=None,
        chat_id="oc_1",
        source_message_id="om_bind",
        ttl_minutes=30,
    )
    setup_session = await store.create_setup_session(
        team_id="team_1",
        workspace_id="ws_1",
        created_by_user_id="usr_admin",
        tenant_key="tenant_1",
        expires_in_seconds=1800,
    )
    await store.upsert_turn_link(
        turn_id="turn_old",
        session_id="sess_old",
        tenant_key="tenant_1",
        chat_id="oc_1",
        sender_open_id="ou_1",
        source_message_id="om_turn",
    )
    assert await store.claim_message(event_id="evt_old", message_id="om_old") is True

    now = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)
    very_old = (now - timedelta(days=45)).isoformat(timespec="seconds")
    old_dedup = (now - timedelta(days=20)).isoformat(timespec="seconds")

    async with open_shared_ctx(store.db_path) as db:
        await db.execute(
            "UPDATE feishu_binding_sessions SET expires_at=?, created_at=?, updated_at=? WHERE id=?",
            (very_old, very_old, very_old, binding_session.id),
        )
        await db.execute(
            "UPDATE feishu_setup_sessions SET expires_at=?, created_at=?, updated_at=? WHERE id=?",
            (very_old, very_old, very_old, setup_session.id),
        )
        await db.execute(
            "UPDATE feishu_message_dedup SET received_at=? WHERE dedup_key=?",
            (old_dedup, "om_old"),
        )
        await db.execute(
            "UPDATE feishu_turn_links SET created_at=?, updated_at=? WHERE turn_id=?",
            (very_old, very_old, "turn_old"),
        )
        await db.commit()

    stats = await store.sweep_hygiene(now=now)

    assert stats.expired_binding_sessions == 1
    assert stats.pruned_binding_sessions == 1
    assert stats.expired_setup_sessions == 1
    assert stats.pruned_setup_sessions == 1
    assert stats.pruned_message_dedup == 1
    assert stats.pruned_turn_links == 1

    assert await store.get_binding_session(binding_session.id) is None
    assert await store.get_setup_session(setup_session.id) is None
    assert await store.get_turn_link("turn_old") is None
    assert await store.get_message_status("om_old") is None


@pytest.mark.asyncio
async def test_feishu_event_store_hygiene_sweep_keeps_recent_rows(tmp_path) -> None:
    store = FeishuEventStore(tmp_path / "feishu.db")
    await store.initialize()

    binding_session = await store.create_binding_session(
        tenant_key="tenant_1",
        open_id="ou_1",
        union_id=None,
        email=None,
        chat_id="oc_1",
        source_message_id="om_bind",
        ttl_minutes=30,
    )
    setup_session = await store.create_setup_session(
        team_id="team_1",
        workspace_id="ws_1",
        created_by_user_id="usr_admin",
        tenant_key="tenant_1",
        expires_in_seconds=1800,
    )
    await store.upsert_turn_link(
        turn_id="turn_recent",
        session_id="sess_recent",
        tenant_key="tenant_1",
        chat_id="oc_1",
        sender_open_id="ou_1",
        source_message_id="om_turn",
    )
    assert await store.claim_message(event_id="evt_recent", message_id="om_recent") is True

    now = datetime.now(tz=UTC)
    stats = await store.sweep_hygiene(now=now)

    assert stats.total_mutations == 0
    assert await store.get_binding_session(binding_session.id) is not None
    assert await store.get_setup_session(setup_session.id) is not None
    assert await store.get_turn_link("turn_recent") is not None
    assert await store.get_message_status("om_recent") == "received"


def test_verify_feishu_workspace_launch_gate_fails_team_scope_on_missing_datarc_mapping(
    tmp_path,
) -> None:
    db_path = tmp_path / "launch_gate.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL
            );
            CREATE TABLE workspace_members (
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                name TEXT,
                datarc_workspace_id TEXT,
                status TEXT NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO users(id, team_id) VALUES('usr_1', 'team_1')")
        conn.execute("INSERT INTO workspaces(id, team_id, name, datarc_workspace_id, status) VALUES('ws_1', 'team_1', 'Demo', NULL, 'active')")
        conn.execute("INSERT INTO workspace_members(workspace_id, user_id, status) VALUES('ws_1', 'usr_1', 'active')")
        conn.commit()
    finally:
        conn.close()

    script = Path("scripts/verify_feishu_workspace_launch_gate.py")
    result = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--team-id", "team_1", "--require-ready"],
        cwd=Path(__file__).resolve().parents[3],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ready_for_feishu_workspace_launch_gate"] is False
    assert report["scoped_workspaces_missing_datarc_mapping_count"] == 1
    assert report["scoped_workspaces_missing_datarc_mapping"][0]["workspace_id"] == "ws_1"


async def _write_app_config(store: FeishuEventStore, *, app_id: str) -> None:
    await store.upsert_app_config(
        team_id="team_1",
        tenant_key="",
        app_type="internal",
        enabled=True,
        event_ingress="webhook",
        app_id=app_id,
        app_secret_ref=None,
        verification_token_ref=None,
        encrypt_key_ref=None,
        bot_open_id="ou_bot",
        group_public_answers_enabled=False,
        actor_user_id="usr_admin",
    )

"""Batch-A max-schema fixture and rollback-image compatibility drill.

This module intentionally uses only runtime dependencies so the exact same
command can run inside pinned application images.  Release admission prepares
the fixture with the candidate image, verifies it with the prebuilt rollback
image, then verifies the mutated database again with the candidate image.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx2
import jwt
from bowhead_core._db import open_shared_ctx
from bowhead_core.clarification_store import ClarificationCandidate, ClarificationStore
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.public_entries import PublicEntryStore
from bowhead_core.session_store import SessionStore
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app

from bowhead_cli.migrate import upgrade
from bowhead_router import AgentBinding, ProviderConfig, ProviderRegistry, Router

# Do not hardcode a revision number: the rollback drill would report an **outdated head** (the very reason
# test_migration_head_pins_are_consistent was introduced).
_HEAD = chain_head()
_KEY_BYTES = b"composer-a-rollback-fixture-key!"
_KEY_B64 = base64.urlsafe_b64encode(_KEY_BYTES).decode().rstrip("=")
_FERNET_ROOT_B64 = base64.urlsafe_b64encode(b"composer-a-rollback-fernet-root!").decode()
_KEY_ID = hashlib.sha256(b"bowhead/chat-submission-key-id/v1\0" + _KEY_BYTES).hexdigest()
_AUTH_HASH = "a" * 64
_USER_ID = "u_composer_rollback_fixture"
_TEAM_ID = "team_composer_rollback_fixture"
_WORKSPACE_ID = "ws_composer_rollback_fixture"
_TELEMETRY_EVENT_ID = "1b6b73b0-36fa-4e04-bf67-eab0c5ac4476"
_CLARIFICATION_REQUEST_ID = "clarification_composer_rollback_fixture"
_PENDING_CLARIFICATION_TOKEN = "composer-rollback-pending-work"
_PENDING_CLARIFICATION_TURN_ID = "t_composer_rollback_pending_clarification"
_PENDING_CLARIFICATION_SESSION_ID = "s_composer_rollback_pending_clarification"
_BUNDLE_REF = {
    "fileId": "art_rollback_bundle",
    "filename": "rollback-bundle.zip",
    "size": 42,
    "mime": "application/zip",
    "bundleArtifactIds": ["art_rollback_child_a", "art_rollback_child_b"],
}
_REQUIRED_TABLES = frozenset(
    {
        "chat_auth_session_generations",
        "chat_auth_user_generation_fences",
        "chat_submission_abandon_tombstones",
        "chat_submission_inflight",
        "chat_submission_receipts",
        "chat_turn_spawn_dispatch",
        "chat_turn_user_sources",
        "clarification_resolution_outbox",
        "security_key_epochs",
        "turn_cancel_receipts",
    },
)


def _submission_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"bowhead/composer-a-rollback/{label}"))


def _team_config() -> dict[str, object]:
    binding = {
        "primary": {"provider": "team", "model": "team-model"},
        "fallback": [],
    }
    return {
        "schema_version": 2,
        "providers": {
            "team": {
                "type": "openai_compatible",
                "base_url": "https://93.184.216.34/v1",
                "api_key": "rollback-drill-never-sent",
                "models": ["team-model"],
            },
        },
        "agents": {
            "lead": binding,
            "modeler": binding,
            "analyst": {"default": binding, "profiles": {}},
            "narrative": binding,
            "echo": binding,
            "judge": binding,
        },
    }


def _mock_team_router() -> Router:
    registry = ProviderRegistry()
    registry.load([ProviderConfig(name="team", type="mock")])
    router = Router(registry)
    for agent_type in ("lead", "modeler", "analyst", "narrative", "echo", "judge"):
        router.bind(
            AgentBinding(
                agent_type=agent_type,
                primary=("team", "team-model"),
            ),
        )
    return router


class _StaticTeamRouterRegistry:
    def __init__(self, router: Router) -> None:
        self.router = router

    async def get(self, *_args: object, **_kwargs: object) -> Router:
        return self.router


def _assert_schema(db_path: Path) -> str:
    with sqlite3.connect(db_path) as connection:
        head_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        head = str(head_row[0]) if head_row and head_row[0] else ""
        # The migration head known to this image is the compatibility floor; the rollback image must accept
        # newer, numerically higher revisions written by the candidate, while rejecting older databases missing this image's schema.
        try:
            head_num = int(head.split("_", 1)[0])
        except ValueError:
            head_num = -1
        if head_num < int(_HEAD.split("_", 1)[0]):
            raise RuntimeError(f"unexpected migration head: {head_row!r}")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        }
        missing = sorted(_REQUIRED_TABLES - tables)
        if missing:
            raise RuntimeError(f"rollback image cannot read required tables: {missing}")
    return head


async def _prepare(db_path: Path) -> dict[str, object]:
    if db_path.exists():
        raise RuntimeError("fixture path must not already exist")
    if db_path.name != "bowhead.db":
        raise RuntimeError("fixture must be named bowhead.db for the multi-target migration runner")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    # Migrate **all** target databases: Gateway startup opens the KB databases (e.g. kb_files' kb_rebuild_jobs); a fixture that migrates only core
    # fails with no such table when Gateway starts. Core already has the 1.0.0 baseline schema, so upgrading it to head here is idempotent.
    upgrade(f"sqlite+aiosqlite:///{db_path}")

    store = SessionStore(db_path)
    await store.initialize()
    users = UserStore(db_path)
    await users.initialize()
    clarification_team = await users.create_team(
        name="Composer rollback frozen clarification",
        max_seats=1,
    )
    clarification_user = await users.create_user(
        team_id=clarification_team.id,
        email="composer-rollback-frozen-clarification@example.com",
        display_name="Composer rollback frozen clarification",
        tier="ultra",
        email_verified=True,
    )
    await users.set_team_creator(clarification_team.id, clarification_user.id)
    clarification_workspace_id = await users.create_local_workspace(
        team_id=clarification_team.id,
        name="Composer rollback frozen clarification",
        creator_user_id=clarification_user.id,
    )
    await store.register_chat_auth_session_generation(
        user_id=_USER_ID,
        auth_session_generation_hash=_AUTH_HASH,
        issued_at_epoch=int(datetime.now(tz=UTC).timestamp()),
    )
    if not await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(_KEY_ID,),
    ):
        raise RuntimeError("failed to seed submission fingerprint epoch")

    receipt_id = _submission_id("prepared-receipt")
    common = {
        "team_id": _TEAM_ID,
        "user_id": _USER_ID,
        "client_submission_id": receipt_id,
        "workspace_id": _WORKSPACE_ID,
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "1" * 64,
        "accepted_request_fingerprints": ("1" * 64,),
        "fingerprint_key_id": _KEY_ID,
        "auth_session_generation_hash": _AUTH_HASH,
    }
    claim = await store.claim_chat_submission(**common)
    if claim.kind != "claimed" or claim.lease_owner is None or claim.lease_version is None:
        raise RuntimeError(f"fixture claim failed: {claim.kind}")
    await store.finalize_started_chat_submission(
        **{
            key: value
            for key, value in common.items()
            if key
            not in {
                "accepted_request_fingerprints",
                "fingerprint_key_id",
                "auth_session_generation_hash",
            }
        },
        actual_session_id="s_composer_rollback_prepared",
        turn_id="t_composer_rollback_prepared",
        user_text="rollback fixture",
        attachment_ids=["art_rollback_child_a", "art_rollback_child_b"],
        attachment_refs=[_BUNDLE_REF],
        parts=[
            {
                "text": "rollback fixture",
                "attachments": [_BUNDLE_REF],
            },
        ],
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        response={
            "session_id": "s_composer_rollback_prepared",
            "turn_id": "t_composer_rollback_prepared",
            "stream_url": "/api/v1/chat/stream/t_composer_rollback_prepared",
            "status": "started",
        },
    )
    await store.request_cancel_turn(
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        cancel_request_id=_submission_id("prepared-cancel"),
        workspace_id=_WORKSPACE_ID,
        turn_id="t_composer_rollback_prepared",
        expected_execution_generation=1,
    )
    clarification_store = ClarificationStore(db_path)
    await clarification_store.record_resolution_outcome(
        request_id=_CLARIFICATION_REQUEST_ID,
        turn_id="t_composer_rollback_prepared",
        payload={
            "request_id": _CLARIFICATION_REQUEST_ID,
            "chosen_value": "rollback-choice",
            "chosen_values": ["rollback-choice"],
        },
    )
    await store.ensure_session(
        session_id=_PENDING_CLARIFICATION_SESSION_ID,
        workspace_id=clarification_workspace_id,
        user_id=clarification_user.id,
    )
    await store.record_turn(
        turn_id=_PENDING_CLARIFICATION_TURN_ID,
        session_id=_PENDING_CLARIFICATION_SESSION_ID,
        user_text="rollback pending clarification",
    )
    await store.mark_clarification_pending(
        turn_id=_PENDING_CLARIFICATION_TURN_ID,
        intent="rollback pending clarification",
    )
    pending_resolution = await clarification_store.put(
        turn_id=_PENDING_CLARIFICATION_TURN_ID,
        user_id=clarification_user.id,
        workspace_id=clarification_workspace_id,
        token=_PENDING_CLARIFICATION_TOKEN,
        rationale="rollback must recover durable clarification work",
        candidates=[
            ClarificationCandidate(
                value="pending-choice",
                label="Pending choice",
            ),
        ],
    )
    _, transitioned = await clarification_store.resolve_once(
        pending_resolution.request_id,
        "pending-choice",
        resolution_channel="web",
        resolution_work={
            # Exact frozen Gateway work envelope. Rollback compatibility must
            # preserve the decoder input, not a look-alike result payload.
            "payload": {
                "chosen_value": "pending-choice",
                "chosen_values": ["pending-choice"],
                "kind": "measure",
            },
        },
        resolution_owner="candidate-owner",
    )
    if not transitioned:
        raise RuntimeError("clarification pending-work fixture was not resolved")
    # Model a candidate crash after the atomic pending→resolved + work insert.
    # The rollback image must claim this expired lease and finalize the same row.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE clarification_resolution_outbox SET lease_expires_at='1970-01-01T00:00:00+00:00' WHERE request_id=? AND state='pending'",
            (pending_resolution.request_id,),
        )
        connection.commit()

    pending = dict(common)
    pending.update(
        client_submission_id=_submission_id("prepared-pending"),
        request_fingerprint="2" * 64,
        accepted_request_fingerprints=("2" * 64,),
    )
    pending_claim = await store.claim_chat_submission(**pending)
    if pending_claim.kind != "claimed":
        raise RuntimeError(f"pending fixture claim failed: {pending_claim.kind}")

    abandoned = dict(common)
    abandoned.update(
        client_submission_id=_submission_id("prepared-abandoned"),
        request_fingerprint="3" * 64,
        accepted_request_fingerprints=("3" * 64,),
    )
    abandoned_claim = await store.claim_chat_submission(**abandoned)
    if abandoned_claim.kind != "claimed":
        raise RuntimeError(f"abandon fixture claim failed: {abandoned_claim.kind}")
    outcome = await store.abandon_chat_submission(
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        client_submission_id=str(abandoned["client_submission_id"]),
        workspace_id=_WORKSPACE_ID,
        requested_session_id=None,
    )
    if outcome != "abandoned":
        raise RuntimeError(f"abandon fixture failed: {outcome!r}")

    recorded = await store.record_composer_telemetry_event(
        user_id=_USER_ID,
        event_id=_TELEMETRY_EVENT_ID,
        occurred_at=datetime.now(tz=UTC),
    )
    if not recorded:
        raise RuntimeError("telemetry fixture was not recorded")

    database_head = _assert_schema(db_path)
    return {"phase": "prepare", "head": database_head, "receipt": receipt_id}


def _store_submission(label: str, fingerprint: str) -> dict[str, object]:
    return {
        "team_id": _TEAM_ID,
        "user_id": _USER_ID,
        "client_submission_id": _submission_id(label),
        "workspace_id": _WORKSPACE_ID,
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": fingerprint,
        "accepted_request_fingerprints": (fingerprint,),
        "fingerprint_key_id": _KEY_ID,
        "auth_session_generation_hash": _AUTH_HASH,
    }


async def _verify_seeded_protocol_state(
    db_path: Path,
    store: SessionStore,
    *,
    phase: str,
) -> None:
    source = await store.get_chat_turn_user_source(
        turn_id="t_composer_rollback_prepared",
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
    )
    if (
        source is None
        or source.attachment_ids != ("art_rollback_child_a", "art_rollback_child_b")
        or source.attachment_refs != (_BUNDLE_REF,)
        or source.parts
        != (
            {
                "text": "rollback fixture",
                "attachments": [_BUNDLE_REF],
            },
        )
    ):
        raise RuntimeError("rollback image lost the prepared typed attachment source")

    cancel = await store.get_cancel_turn_receipt(
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        cancel_request_id=_submission_id("prepared-cancel"),
        turn_id="t_composer_rollback_prepared",
        expected_execution_generation=1,
    )
    if cancel is None or cancel.outcome != "cancelled":
        raise RuntimeError("rollback image lost the prepared cancel receipt")

    abandoned = _store_submission("prepared-abandoned", "3" * 64)
    abandoned_claim = await store.claim_chat_submission(**abandoned)
    if abandoned_claim.kind != "abandoned":
        raise RuntimeError(
            f"rollback image lost the prepared abandon fence: {abandoned_claim.kind}",
        )

    pending = _store_submission("prepared-pending", "2" * 64)
    pending_claim = await store.claim_chat_submission(**pending)
    if pending_claim.kind not in {"pending", "claimed", "abandoned"}:
        raise RuntimeError(
            f"rollback image could not read the prepared inflight row: {pending_claim.kind}",
        )
    if pending_claim.kind != "abandoned":
        outcome = await store.abandon_chat_submission(
            team_id=_TEAM_ID,
            user_id=_USER_ID,
            client_submission_id=str(pending["client_submission_id"]),
            workspace_id=_WORKSPACE_ID,
            requested_session_id=None,
        )
        if outcome != "abandoned":
            raise RuntimeError(f"rollback image could not abandon prepared inflight: {outcome}")
    pending_after = await store.claim_chat_submission(**pending)
    if pending_after.kind != "abandoned":
        raise RuntimeError("rollback image did not preserve the inflight abandonment fence")

    with sqlite3.connect(db_path) as connection:
        telemetry = connection.execute(
            "SELECT occurred_at FROM composer_telemetry_dedupe WHERE user_id=? AND event_id=?",
            (_USER_ID, _TELEMETRY_EVENT_ID),
        ).fetchone()
    if telemetry is None:
        raise RuntimeError("rollback image lost the prepared telemetry fence")
    duplicate = await store.record_composer_telemetry_event(
        user_id=_USER_ID,
        event_id=_TELEMETRY_EVENT_ID,
        occurred_at=datetime.fromisoformat(str(telemetry[0])),
    )
    if duplicate:
        raise RuntimeError("rollback image duplicated the prepared telemetry event")

    clarification_store = ClarificationStore(db_path)
    with sqlite3.connect(db_path) as connection:
        pending_row = connection.execute(
            "SELECT request_id FROM clarification_requests WHERE turn_id=? AND token=?",
            (_PENDING_CLARIFICATION_TURN_ID, _PENDING_CLARIFICATION_TOKEN),
        ).fetchone()
    if pending_row is None:
        raise RuntimeError("rollback image lost pending clarification work identity")
    pending_request_id = str(pending_row[0])
    work_state = await clarification_store.get_resolution_work_state(pending_request_id)
    if phase.startswith("rollback") and work_state != "pending":
        raise RuntimeError(
            f"rollback image did not receive expired pending clarification work: {work_state!r}",
        )
    if not phase.startswith("rollback") and work_state != "completed":
        raise RuntimeError(
            f"forward image did not receive completed clarification work: {work_state!r}",
        )
    pending_outcome = await clarification_store.get_resolution_outcome(
        pending_request_id,
    )
    if work_state == "pending" and pending_outcome is not None:
        raise RuntimeError("pending clarification work unexpectedly has a completed outcome")
    if work_state == "completed" and (pending_outcome is None or pending_outcome.get("chosen_value") != "pending-choice"):
        raise RuntimeError("completed clarification work lost its canonical outcome")


def _restores_process_env(fn):
    """跑完把进程 env 还原成调用前的样子。

    这支 drill 原本独占一个进程（发布准入时单独起），所以直接写 `os.environ` 没问题。
    但它同时被 `bowhead_cli/tests/test_composer_a_rollback_drill.py` **在进程内**调用，
    于是留下的 `BOWHEAD_DEV_USER_FALLBACK=0` 会毒死同一个 pytest worker 上后续所有
    gateway 用例：`bowhead_gateway/tests/conftest.py` 的 autouse fixture 当时是条件式的
    （`if "BOWHEAD_DEV_USER_FALLBACK" not in os.environ`，已在修这条时改成无条件复位），
    变量已存在就不再设成 "1"，`_dev_user_fallback_enabled()` 随之恒 False，不带 token
    的请求全部 401。哪些用例排在它后面由 xdist 调度决定，所以 `make test` 每次红一批
    **不同**的用例（实测 23/27/51/57/67/111 都出现过，失败集互不相交），而单跑全绿。

    确定性复现：`pytest <本文件的测试> <gateway/tests/test_memory_routes_v3.py>`
    → 58 failed；后者单跑 → 59 passed。

    ★ 快照**整个** `os.environ` 而不是一串手维护的键名：后者要求「往下面的
      `os.environ.update` 里加键时必须同步改这里」，漏改不会有任何东西变红——
      正是这条修复要消灭的那种静默失效形状。函数返回后只剩 `_assert_schema(db_path)`
      （显式传参、不读 env），所以整体还原是安全的。
    """

    @functools.wraps(fn)
    async def _wrapped(*args, **kwargs):
        snapshot = dict(os.environ)
        try:
            return await fn(*args, **kwargs)
        finally:
            for key in set(os.environ) - set(snapshot):
                os.environ.pop(key, None)
            for key, value in snapshot.items():
                if os.environ.get(key) != value:
                    os.environ[key] = value

    return _wrapped


@_restores_process_env
async def _verify_gateway_contract(db_path: Path, phase: str) -> None:
    """Exercise capability, team fail-closed and typed wire through the real app."""
    jwt_secret = "composer-a-rollback-jwt-secret-32-bytes"
    os.environ.update(
        {
            "BOWHEAD_DB_URL": f"sqlite+aiosqlite:///{db_path}",
            "BOWHEAD_ENV": "dev",
            "BOWHEAD_DEPLOYMENT_MODE": "saas",
            "BOWHEAD_AUTH_MODE": "dev",
            "BOWHEAD_DEV_USER_FALLBACK": "0",
            "BOWHEAD_ENTITLEMENT_ENFORCE": "0",
            "BOWHEAD_VECTOR_DB_ENABLED": "0",
            "BOWHEAD_SUBMISSION_FINGERPRINT_KEY": _KEY_B64,
            "BOWHEAD_JWT_SECRET": jwt_secret,
            # Team chat turns construct the reference cipher from the instance
            # Fernet root (16e44bbc5); production always provides one.
            "BOWHEAD_FERNET_KEY": _FERNET_ROOT_B64,
            # The Fernet root also enables the privacy ledger; keep it beside
            # the drill database instead of the image default under /app.
            "BOWHEAD_PRIVACY_LEDGER_DB": f"{db_path}.privacy",
            # With a Fernet root present the reference runtime defaults on;
            # the rollback contract below expects every composer flag off. A
            # real rollback sets the same explicit 0 (see the default-on batch
            # section of docs/灰度功能清单.md); this drill mirrors that runbook.
            "FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME": "0",
        },
    )
    os.environ.pop("BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS", None)
    os.environ.pop("BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL", None)

    users = UserStore(db_path)
    await users.initialize()
    team = await users.create_team(name=f"Composer rollback {phase}", max_seats=2)
    user = await users.create_user(
        team_id=team.id,
        email=f"composer-rollback-{phase}@example.com",
        display_name=f"Composer rollback {phase}",
        tier="ultra",
        email_verified=True,
    )
    await users.set_team_creator(team.id, user.id)
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name=f"Composer rollback {phase}",
        creator_user_id=user.id,
    )
    entries = PublicEntryStore(db_path)
    await entries.initialize()
    await entries.create_bootstrap_active(
        team_id=team.id,
        base_url="http://localhost:8090",
        actor_id=user.id,
        verification_method="trusted_proxy_bootstrap",
    )
    with sqlite3.connect(db_path) as connection:
        pending_identity = connection.execute(
            "SELECT c.request_id,c.user_id,c.workspace_id,u.team_id "
            "FROM clarification_requests c JOIN users u ON u.id=c.user_id "
            "WHERE c.turn_id=? AND c.token=?",
            (_PENDING_CLARIFICATION_TURN_ID, _PENDING_CLARIFICATION_TOKEN),
        ).fetchone()
    if pending_identity is None:
        raise RuntimeError("rollback Gateway lost frozen clarification route identity")
    pending_request_id, pending_user_id, pending_workspace_id, pending_team_id = map(
        str,
        pending_identity,
    )

    token = jwt.encode(
        {
            "sub": user.id,
            "team_id": team.id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "pwd_v": 0,
        },
        jwt_secret,
        algorithm="HS256",
    )
    pending_token = jwt.encode(
        {
            "sub": pending_user_id,
            "team_id": pending_team_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "pwd_v": 0,
        },
        jwt_secret,
        algorithm="HS256",
    )
    failed_submission_id = str(uuid4())
    failed_payload = {
        "workspace_id": workspace_id,
        "message": f"{phase} must fail before a team model exists",
        "client_info": {"composer_surface": "home"},
        "client_submission_id": failed_submission_id,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
    }
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://localhost:8090",
            cookies={"bowhead_jwt": token},
        ) as client:
            me = await client.get("/api/v1/auth/me")
            if me.status_code != 200:
                raise RuntimeError(f"rollback Gateway auth failed: {me.text}")
            composer = me.json().get("capabilities", {}).get("composer")
            # All composer capability flags must be off after rollback, including the later quick-reference, reference-Plan and structured-onboarding flags.
            if composer != {
                "model_panel_v2": False,
                "thinking_effort": False,
                "submission_protocol": False,
                "reference_input": False,
                "reference_plan": False,
                "agent_start_use": False,
            }:
                raise RuntimeError(f"rollback Gateway capability contract drifted: {composer!r}")

            rejected = await client.post("/api/v1/chat/send", json=failed_payload)
            error_code = rejected.json().get("detail", {}).get("error", {}).get("code")
            if rejected.status_code != 409 or error_code != "team_model_unconfigured":
                raise RuntimeError(
                    f"rollback Gateway did not fail closed without a team model: {rejected.status_code} {rejected.text}",
                )

            await users.update_team_llm_config(team.id, _team_config())
            app.state.team_router_registry = _StaticTeamRouterRegistry(_mock_team_router())
            bundle = await app.state.artifact_store.put_bytes(
                content=b"bundle",
                filename="rollback-bundle.zip",
                mime="application/zip",
                created_by=user.id,
            )
            child_a = await app.state.artifact_store.put_bytes(
                content=b"a,b\n1,2\n",
                filename="rollback-a.csv",
                mime="text/csv",
                created_by=user.id,
            )
            child_b = await app.state.artifact_store.put_bytes(
                content=b"a,b\n3,4\n",
                filename="rollback-b.csv",
                mime="text/csv",
                created_by=user.id,
            )
            async with open_shared_ctx(db_path) as connection:
                await connection.executemany(
                    "INSERT INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,session_id,created_at) VALUES (?,?,?,?,datetime('now'))",
                    [(artifact.id, workspace_id, user.id, None) for artifact in (bundle, child_a, child_b)],
                )
                await connection.executemany(
                    "INSERT INTO chat_attachment_bundle_members(bundle_artifact_id,child_artifact_id,created_at) VALUES (?,?,datetime('now'))",
                    [(bundle.id, child_a.id), (bundle.id, child_b.id)],
                )
                await connection.commit()

            submission_id = str(uuid4())
            typed_payload = {
                "workspace_id": workspace_id,
                "message": f"{phase} typed attachment contract",
                "client_info": {"composer_surface": "home"},
                "client_submission_id": submission_id,
                "submission_kind": "start",
                "submission_contract_version": "chat-submit.v1",
                "model_control_batch": "a",
                "attachments": [child_a.id, child_b.id],
                "attachment_refs": [
                    {
                        "fileId": bundle.id,
                        "filename": bundle.filename,
                        "size": bundle.size,
                        "mime": bundle.mime,
                        "bundleArtifactIds": [child_a.id, child_b.id],
                    },
                ],
            }
            first = await client.post("/api/v1/chat/send", json=typed_payload)
            replay = await client.post("/api/v1/chat/send", json=typed_payload)
            if first.status_code != 200 or replay.json() != first.json():
                raise RuntimeError(
                    f"rollback Gateway typed send/replay failed: {first.text} / {replay.text}",
                )
            receipt = await client.get(
                f"/api/v1/chat/submissions/{submission_id}",
                params={"workspace_id": workspace_id},
            )
            session = await client.get(f"/api/v1/sessions/{first.json()['session_id']}")
            if receipt.status_code != 200 or receipt.json().get("response") != first.json():
                raise RuntimeError("rollback Gateway did not expose the typed send receipt")
            expected_attachment = {
                "file_id": bundle.id,
                "filename": bundle.filename,
                "mime": bundle.mime,
                "size": bundle.size,
                "bundle_artifact_ids": [child_a.id, child_b.id],
            }
            turns = session.json().get("turns", []) if session.status_code == 200 else []
            if not turns or turns[0].get("attachments") != [expected_attachment]:
                raise RuntimeError(
                    f"rollback Gateway lost typed attachment hydration: {session.text}",
                )

        pending_transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=pending_transport,
            base_url="http://localhost:8090",
            cookies={"bowhead_jwt": pending_token},
        ) as pending_client:
            resolve_url = f"/api/v1/clarifications/{pending_request_id}/resolve"
            first_resolution = await pending_client.post(
                resolve_url,
                json={"chosen_value": "retry-must-not-win", "kind": "dimension"},
            )
            replay_resolution = await pending_client.post(
                resolve_url,
                json={"chosen_value": "another-loser", "kind": "dimension"},
            )
            expected_resolution = {
                "request_id": pending_request_id,
                "chosen_value": "pending-choice",
                "chosen_values": ["pending-choice"],
                "workspace_id": pending_workspace_id,
            }
            if first_resolution.status_code != 200:
                raise RuntimeError(
                    f"rollback Gateway could not recover frozen clarification work: {first_resolution.status_code} {first_resolution.text}",
                )
            if replay_resolution.status_code != 200:
                raise RuntimeError(
                    f"rollback Gateway could not replay recovered clarification work: {replay_resolution.status_code} {replay_resolution.text}",
                )
            for response_json in (first_resolution.json(), replay_resolution.json()):
                if any(response_json.get(key) != value for key, value in expected_resolution.items()):
                    raise RuntimeError(
                        f"rollback Gateway clarification canonical outcome drifted: {response_json!r}",
                    )

    with sqlite3.connect(db_path) as connection:
        failed_rows = connection.execute(
            "SELECT (SELECT COUNT(*) FROM chat_submission_receipts "
            "WHERE client_submission_id=?),"
            "(SELECT COUNT(*) FROM chat_submission_inflight "
            "WHERE client_submission_id=?)",
            (failed_submission_id, failed_submission_id),
        ).fetchone()
        clarification_row = connection.execute(
            "SELECT state,payload_json FROM clarification_resolution_outbox WHERE request_id=?",
            (pending_request_id,),
        ).fetchone()
        clarification_event_count = connection.execute(
            "SELECT COUNT(*) FROM turn_events WHERE id=?",
            (f"evt_clarification_resolution_{pending_request_id}",),
        ).fetchone()
    if failed_rows != (0, 0):
        raise RuntimeError(f"team-model fail-closed request mutated submission state: {failed_rows}")
    if clarification_row is None or str(clarification_row[0]) != "completed":
        raise RuntimeError("rollback Gateway did not complete frozen clarification work")
    clarification_payload = json.loads(str(clarification_row[1]))
    if clarification_payload.get("chosen_value") != "pending-choice":
        raise RuntimeError("rollback Gateway did not preserve the frozen clarification winner")
    if clarification_event_count != (1,):
        raise RuntimeError(
            f"rollback Gateway duplicated the durable clarification event: {clarification_event_count}",
        )


async def _verify(db_path: Path, phase: str) -> dict[str, object]:
    _assert_schema(db_path)
    store = SessionStore(db_path)
    if not await store.chat_submission_protocol_schema_ready():
        raise RuntimeError("rollback image rejected the batch-A schema")
    if not await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(_KEY_ID,),
    ):
        raise RuntimeError("rollback image rejected the persisted key epoch")
    await _verify_seeded_protocol_state(db_path, store, phase=phase)
    prepared = await store.get_chat_submission_receipt(
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        client_submission_id=_submission_id("prepared-receipt"),
        workspace_id=_WORKSPACE_ID,
        requested_session_id=None,
    )
    if prepared is None or prepared.outcome != "started":
        raise RuntimeError("rollback image could not replay the prepared receipt")
    clarification_outcome = await ClarificationStore(db_path).get_resolution_outcome(
        _CLARIFICATION_REQUEST_ID,
    )
    if clarification_outcome is None or clarification_outcome.get("chosen_value") != "rollback-choice":
        raise RuntimeError("rollback image could not replay the clarification outcome")

    suffix = uuid5(NAMESPACE_URL, f"bowhead/composer-a-rollback/verify/{phase}").hex[:12]
    submission_id = _submission_id(f"verify-{phase}")
    session_id = f"s_rollback_{suffix}"
    turn_id = f"t_rollback_{suffix}"
    common = {
        "team_id": _TEAM_ID,
        "user_id": _USER_ID,
        "client_submission_id": submission_id,
        "workspace_id": _WORKSPACE_ID,
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "4" * 64,
        "accepted_request_fingerprints": ("4" * 64,),
        "fingerprint_key_id": _KEY_ID,
        "auth_session_generation_hash": _AUTH_HASH,
    }
    claim = await store.claim_chat_submission(**common)
    if claim.kind == "claimed":
        assert claim.lease_owner is not None and claim.lease_version is not None
        await store.finalize_started_chat_submission(
            **{
                key: value
                for key, value in common.items()
                if key
                not in {
                    "accepted_request_fingerprints",
                    "fingerprint_key_id",
                    "auth_session_generation_hash",
                }
            },
            actual_session_id=session_id,
            turn_id=turn_id,
            user_text=f"{phase} legacy chat",
            attachment_ids=[],
            lease_owner=claim.lease_owner,
            lease_version=claim.lease_version,
            response={
                "session_id": session_id,
                "turn_id": turn_id,
                "stream_url": f"/api/v1/chat/stream/{turn_id}",
                "status": "started",
            },
        )
    elif claim.kind != "receipt":
        raise RuntimeError(f"rollback image could not claim/replay a legacy send: {claim.kind}")

    replay = await store.claim_chat_submission(**common)
    if replay.kind != "receipt" or replay.receipt is None:
        raise RuntimeError(f"rollback image did not replay same-ID send: {replay.kind}")
    dispatch_owner_id = f"rollback-drill:{suffix}"
    dispatch_claim = await store.claim_chat_spawn_dispatch_for_worker(
        turn_id=turn_id,
        owner_id=dispatch_owner_id,
    )
    if dispatch_claim != "claimed":
        raise RuntimeError(
            f"rollback image could not claim the parent dispatch: {dispatch_claim}",
        )
    appended = await store.append_steer_buffer(
        session_id=session_id,
        text=f"{phase} accepted follow-up",
        expected_turn_id=turn_id,
    )
    if not appended:
        raise RuntimeError("rollback image did not accept the follow-up")
    if not await store.mark_completed(
        turn_id=turn_id,
        intent=None,
        final_answer="rollback drill parent terminal",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0.0,
    ):
        raise RuntimeError("rollback image did not persist the parent terminal fact")
    turn_log = TurnLogStore(db_path)
    await turn_log.initialize()
    if not await turn_log.record(
        turn_id=turn_id,
        event_type="final_answer",
        payload={
            "text": "rollback drill parent terminal",
            "card_id": turn_id,
            "seq": 1,
            "evt_seq": 1,
        },
    ):
        raise RuntimeError("rollback image did not persist the parent final answer")
    if not await turn_log.record(
        turn_id=turn_id,
        event_type="done",
        payload={
            "turn_id": turn_id,
            "status": "completed",
            "execution_generation": 1,
            "seq": 2,
            "evt_seq": 2,
        },
    ):
        raise RuntimeError("rollback image did not persist the parent terminal wire")
    child_id = f"t_rollback_child_{suffix}"
    buffered = await store.materialize_steer_follow_up(
        session_id=session_id,
        current_turn_id=turn_id,
        next_turn_id=child_id,
    )
    if buffered != [f"{phase} accepted follow-up"]:
        raise RuntimeError(f"rollback image lost accepted follow-up: {buffered!r}")
    source = await store.get_chat_turn_user_source(
        turn_id=child_id,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
    )
    if source is None or source.user_text != buffered[0]:
        raise RuntimeError("rollback image did not persist canonical follow-up source")
    if not await store.mark_chat_spawn_dispatch_terminal(
        turn_id=turn_id,
        owner_id=dispatch_owner_id,
    ):
        raise RuntimeError("rollback image did not settle the exact parent owner")
    if not await store.mark_failed(
        turn_id=child_id,
        error="rollback drill terminal",
        error_kind="rollback_drill",
    ):
        raise RuntimeError("rollback image did not persist the child terminal fact")
    if not await turn_log.record(
        turn_id=child_id,
        event_type="turn_failed",
        payload={
            "turn_id": child_id,
            "reason": "rollback drill terminal",
            "reason_code": "lead_internal_error",
            "seq": 1,
            "evt_seq": 1,
        },
    ):
        raise RuntimeError("rollback image did not persist the child failure wire")
    if not await turn_log.record(
        turn_id=child_id,
        event_type="done",
        payload={
            "turn_id": child_id,
            "status": "failed",
            "error_kind": "rollback_drill",
            "execution_generation": 1,
            "seq": 2,
            "evt_seq": 2,
        },
    ):
        raise RuntimeError("rollback image did not persist the child terminal wire")
    if not await store.mark_chat_spawn_dispatch_terminal(
        turn_id=child_id,
        failed=True,
    ):
        raise RuntimeError("rollback image did not settle the child dispatch")
    await store.finalize_turn(
        session_id=session_id,
        current_turn_id=child_id,
        next_turn_id=None,
    )
    await _verify_gateway_contract(db_path, phase)
    _assert_schema(db_path)
    return {
        "phase": phase,
        "head": _HEAD,
        "same_id_replayed": True,
        "canonical_follow_up": True,
        "seeded_protocol_state": True,
        "gateway_capability_contract": True,
        "gateway_team_model_fail_closed": True,
        "gateway_typed_attachment": True,
        "gateway_clarification_recovery": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--phase", default="rollback")
    args = parser.parse_args()
    result = asyncio.run(
        _prepare(args.db) if args.action == "prepare" else _verify(args.db, args.phase),
    )
    sys.stdout.write(f"{json.dumps(result, sort_keys=True)}\n")


if __name__ == "__main__":
    main()

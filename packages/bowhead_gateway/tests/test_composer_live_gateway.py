"""Batch-A contract through the real Gateway lifespan and SQLite stores."""

from __future__ import annotations

import base64
import sqlite3
import time

import httpx2
import jwt
import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.public_entries import PublicEntryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app

from bowhead_router import AgentBinding, ProviderConfig, ProviderRegistry, Router


def _team_config() -> dict:
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
                "api_key": "content-never-used-by-test",
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

    async def get(self, *_args, **_kwargs) -> Router:
        return self.router


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_gateway_replays_one_sqlite_submission(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{db_path}") == tuple(TARGET_FILES), "迁移未跑到 head"
    fingerprint_key = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
    jwt_secret = "composer-live-jwt-secret-at-least-32-bytes"
    monkeypatch.setenv("BOWHEAD_DB_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BOWHEAD_ENV", "dev")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_ENTITLEMENT_ENFORCE", "0")
    monkeypatch.setenv("BOWHEAD_VECTOR_DB_ENABLED", "0")
    monkeypatch.setenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY", fingerprint_key)
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", jwt_secret)
    # Deployments always set the instance Fernet root; team chat turns build the
    # collaboration cipher from it (16e44bbc5). Do not rely on a checkout .env.
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", base64.urlsafe_b64encode(b"c" * 32).decode())
    # The Fernet root also enables the privacy ledger; keep it beside the test database.
    monkeypatch.setenv("BOWHEAD_PRIVACY_LEDGER_DB", f"{db_path}.privacy")
    # This case covers the composer with every capability off; with a Fernet root the reference
    # runtime defaults on (5a7b5fed7), so pin it off explicitly.
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "0")

    users = UserStore(db_path)
    await users.initialize()

    team = await users.create_team(name="Composer Live", max_seats=2)
    user = await users.create_user(
        team_id=team.id,
        email="composer-live@example.com",
        display_name="Composer Live",
        tier="ultra",
        email_verified=True,
    )
    await users.set_team_creator(team.id, user.id)
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Composer Live Space",
        creator_user_id=user.id,
    )
    await users.update_team_llm_config(team.id, _team_config())
    public_entries = PublicEntryStore(db_path)
    await public_entries.initialize()
    await public_entries.create_bootstrap_active(
        team_id=team.id,
        base_url="http://localhost:8090",
        actor_id=user.id,
        verification_method="trusted_proxy_bootstrap",
    )

    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    app = create_app()
    access_token = jwt.encode(
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
    submission_id = "0da31a6b-2c6e-4fb3-8c80-f034b37d840f"
    payload = {
        "workspace_id": workspace_id,
        "message": "This prompt must be materialized once",
        "client_info": {"composer_surface": "home"},
        "client_submission_id": submission_id,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
    }

    async with app.router.lifespan_context(app):
        app.state.team_router_registry = _StaticTeamRouterRegistry(
            _mock_team_router(),
        )
        bundle = await app.state.artifact_store.put_bytes(
            content=b"bundle",
            filename="quarterly.zip",
            mime="application/zip",
            created_by=user.id,
        )
        child_a = await app.state.artifact_store.put_bytes(
            content=b"a,b\n1,2\n",
            filename="q1.csv",
            mime="text/csv",
            created_by=user.id,
        )
        child_b = await app.state.artifact_store.put_bytes(
            content=b"a,b\n3,4\n",
            filename="q2.csv",
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
        payload["attachments"] = [child_a.id, child_b.id]
        payload["attachment_refs"] = [
            {
                "fileId": bundle.id,
                "filename": bundle.filename,
                "size": bundle.size,
                "mime": bundle.mime,
                "bundleArtifactIds": [child_a.id, child_b.id],
            }
        ]
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://localhost:8090",
            cookies={"bowhead_jwt": access_token},
        ) as client:
            me = await client.get("/api/v1/auth/me")
            first = await client.post("/api/v1/chat/send", json=payload)
            replay = await client.post("/api/v1/chat/send", json=payload)
            receipt = await client.get(
                f"/api/v1/chat/submissions/{submission_id}",
                params={"workspace_id": workspace_id},
            )
            session_detail = await client.get(
                f"/api/v1/sessions/{first.json()['session_id']}",
            )
            # A legacy replay identity must not strip the reference document
            # from a persisted source and execute only its display text.
            from bowhead_core.chat_references import InputDocument
            from bowhead_core.chat_turn_source import ChatTurnUserSource
            from bowhead_core.session_store import _insert_chat_turn_user_source_in_tx

            document = InputDocument.model_validate(
                {
                    "schema_version": 1,
                    "nodes": [
                        {
                            "type": "reference",
                            "node_id": "00000000-0000-4000-8000-000000000001",
                            "resource": {"kind": "skill", "source_kind": "team", "id": "sk_exact", "version": "sha256:" + "a" * 64},
                            "label": "Exact method",
                            "parameters": {},
                        },
                        {"type": "text", "text": " analyze profit"},
                    ],
                }
            )
            async with open_shared_ctx(db_path) as connection:
                await connection.execute("BEGIN IMMEDIATE")
                await connection.execute(
                    "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at) VALUES('s_reference_replay',?,?,datetime('now'),datetime('now'))",
                    (workspace_id, user.id),
                )
                await connection.execute(
                    "INSERT INTO turns(id,session_id,seq,user_text,status,started_at) VALUES('t_reference_replay','s_reference_replay',1,?,'completed',datetime('now'))",
                    (document.display_text(),),
                )
                await _insert_chat_turn_user_source_in_tx(
                    connection,
                    source=ChatTurnUserSource(
                        turn_id="t_reference_replay",
                        session_id="s_reference_replay",
                        user_id=user.id,
                        workspace_id=workspace_id,
                        user_text=document.display_text(),
                        attachment_ids=(),
                        attachment_refs=(),
                        parts=None,
                        input_document=document,
                    ),
                    created_at="2026-09-09T00:00:00+00:00",
                )
                await connection.commit()
            downgrade_id = "ba02bbdf-2a59-4ea2-962a-a4c0f6a869e1"
            downgrade = await client.post(
                "/api/v1/chat/send",
                json={
                    "workspace_id": workspace_id,
                    "session_id": "s_reference_replay",
                    "message": "",
                    "client_submission_id": downgrade_id,
                    "submission_kind": "replay",
                    "submission_contract_version": "chat-submit.v1",
                    "model_control_batch": "a",
                    "replay_source_turn_id": "t_reference_replay",
                },
            )
            assert downgrade.status_code == 409, downgrade.text
            assert downgrade.json() == {
                "detail": {"error": {"code": "unsupported_entry", "message": app.state.error_catalog.render("unsupported_entry", "en-US")}}
            }
            with sqlite3.connect(db_path) as connection:
                assert connection.execute("SELECT COUNT(*) FROM turns WHERE session_id='s_reference_replay'").fetchone() == (1,)
                for table in ("chat_submission_receipts", "chat_submission_inflight"):
                    assert connection.execute(f"SELECT COUNT(*) FROM {table} WHERE client_submission_id=?", (downgrade_id,)).fetchone() == (0,)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert me.status_code == 200, me.text
    assert me.json()["capabilities"]["composer"] == {
        "model_panel_v2": False,
        "reference_input": False,
        "thinking_effort": False,
        "submission_protocol": False,
        # 102362c2e: structured Start using entry depends on reference_input, hence False here too.
        "agent_start_use": False,
        # 14033edd0: manual reference Plan readiness (FEATURE_CHAT_REFERENCE_PLAN, default off).
        "reference_plan": False,
    }
    assert replay.json() == first.json()
    assert receipt.status_code == 200
    assert receipt.json()["response"] == first.json()
    assert session_detail.status_code == 200, session_detail.text
    assert session_detail.headers["cache-control"] == "no-store, private"
    assert session_detail.json()["chat_execution"]["write_protocol"] == ("legacy_v0")
    assert session_detail.json()["turns"][0]["attachments"] == [
        {
            "file_id": bundle.id,
            "filename": bundle.filename,
            "mime": bundle.mime,
            "size": bundle.size,
            "bundle_artifact_ids": [child_a.id, child_b.id],
        }
    ]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id=?",
            (first.json()["turn_id"],),
        ).fetchone() == (1,)
        source = connection.execute(
            "SELECT attachment_ids_json,attachment_refs_json FROM chat_turn_user_sources WHERE turn_id=?",
            (first.json()["turn_id"],),
        ).fetchone()
        assert source is not None
        assert source[0] == f'["{child_a.id}","{child_b.id}"]'
        assert "quarterly.zip" in source[1]
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_submission_receipts WHERE client_submission_id=?",
            (submission_id,),
        ).fetchone() == (1,)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reference_wire_recovery_and_closed_dispatch_with_real_auth_and_sqlite(tmp_path, monkeypatch) -> None:
    import copy
    import json
    from pathlib import Path

    from bowhead_core.chat_references import InputDocument
    from bowhead_gateway.chat_reference_request import REFERENCE_MEDIA_TYPE
    from bowhead_gateway.routes.auth import _auth_session_generation_hash
    from bowhead_gateway.routes.chat import ChatSendRequest, _submission_fingerprint

    cases = json.loads((Path(__file__).resolve().parents[3] / "contracts/chat-references/input-document-v1.json").read_text())
    document = next(case["input"] for case in cases["valid"] if case["name"] == "exact_agent_and_parameterized_skill")
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / TARGET_FILES["core"]
    upgrade(f"sqlite+aiosqlite:///{db_path}")
    jwt_secret = "reference-live-jwt-secret-at-least-32-bytes"
    for key, value in {
        "BOWHEAD_DB_URL": f"sqlite+aiosqlite:///{db_path}",
        "BOWHEAD_ENV": "dev",
        "BOWHEAD_DEPLOYMENT_MODE": "saas",
        "BOWHEAD_AUTH_MODE": "dev",
        "BOWHEAD_DEV_USER_FALLBACK": "0",
        "BOWHEAD_ENTITLEMENT_ENFORCE": "0",
        "BOWHEAD_VECTOR_DB_ENABLED": "0",
        "BOWHEAD_JWT_SECRET": jwt_secret,
        "BOWHEAD_FERNET_KEY": base64.urlsafe_b64encode(b"r" * 32).decode(),
        "FEATURE_COMPOSER_RESOURCE_REFERENCES": "1",
        "BOWHEAD_PRIVACY_LEDGER_DB": str(tmp_path / "privacy-ledger.db"),
        "BOWHEAD_SKILL_MULTIRUNTIME": "1",
        "BOWHEAD_SKILL_INVOKE_COMMAND": "1",
    }.items():
        monkeypatch.setenv(key, value)
    # Exercise the existing derived key, without introducing a new secret.
    monkeypatch.delenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY", raising=False)
    users = UserStore(db_path)
    await users.initialize()
    team = await users.create_team(name="Reference contract", max_seats=3)
    owner = await users.create_user(team_id=team.id, email="ref-owner@example.com", display_name="Owner", tier="ultra", email_verified=True)
    await users.set_team_creator(team.id, owner.id)
    user = await users.create_user(team_id=team.id, email="ref-reader@example.com", display_name="Reader", tier="ultra", email_verified=True)
    workspace_id = await users.create_local_workspace(team_id=team.id, name="Reference Space", creator_user_id=owner.id)
    await users.add_member(workspace_id=workspace_id, user_id=user.id, role="member")
    entries = PublicEntryStore(db_path)
    await entries.initialize()
    await entries.create_bootstrap_active(
        team_id=team.id, base_url="http://localhost:8090", actor_id=owner.id, verification_method="trusted_proxy_bootstrap"
    )
    token = jwt.encode(
        {"sub": user.id, "team_id": team.id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": 0}, jwt_secret, algorithm="HS256"
    )
    payload = {
        "workspace_id": workspace_id,
        "message": InputDocument.model_validate(document).display_text(),
        "input_document": document,
        "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
        "submission_kind": "start",
        "submission_contract_version": "chat-references.v1",
        "model_control_batch": "references",
        "attachments": ["art_unavailable"],
    }
    headers = {"content-type": REFERENCE_MEDIA_TYPE}
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8090", cookies={"bowhead_jwt": token}) as client,
    ):
        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        # Production router must use the raw decoder, before any identity/claim.
        raw = json.dumps(payload)
        invalid = await client.post(
            "/api/v1/chat/send", content=raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 2'), headers=headers
        )
        assert invalid.status_code == 422
        assert invalid.json() == {
            "detail": {"error": {"code": "reference_input_invalid", "message": app.state.error_catalog.render("reference_input_invalid", "en-US")}}
        }
        wrong_media = await client.post("/api/v1/chat/send", json=payload)
        assert wrong_media.status_code == 415
        for kind in ("start", "steer", "replay"):
            candidate = {**payload, "submission_kind": kind}
            if kind == "steer":
                candidate.update(session_id="s_absent", expected_parent_turn_id="t_absent", expected_execution_generation=1)
            elif kind == "replay":
                for field in ("message", "input_document", "attachments"):
                    candidate.pop(field)
                candidate["replay_source_turn_id"] = "t_absent"
            result = await client.post("/api/v1/chat/send", content=json.dumps(candidate), headers=headers)
            assert result.status_code == 409, result.text
            # 776b1564e: preparation rejection carries admission=not_started so clients can release local submission journals.
            assert result.json() == {
                "detail": {
                    "error": {
                        "code": "unsupported_entry",
                        "admission": "not_started",
                        "message": app.state.error_catalog.render("unsupported_entry", "en-US"),
                    }
                }
            }
        with sqlite3.connect(db_path) as connection:
            for table in ("turns", "chat_submission_receipts", "chat_submission_inflight"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        generation = me.json()["auth_session_generation"]
        key_id, fingerprint, accepted = _submission_fingerprint(
            ChatSendRequest.model_validate(payload), user_id=user.id, team_id=team.id, auth_session_generation=generation
        )
        common = {
            "team_id": team.id,
            "user_id": user.id,
            "client_submission_id": payload["client_submission_id"],
            "workspace_id": workspace_id,
            "requested_session_id": None,
            "submission_kind": "start",
            "submission_contract_version": "chat-references.v1",
            "model_control_batch": "references",
            "request_fingerprint": fingerprint,
        }
        store = app.state.session_store
        claim = await store.claim_chat_submission(
            **common,
            accepted_request_fingerprints=accepted,
            fingerprint_key_id=key_id,
            auth_session_generation_hash=_auth_session_generation_hash(generation),
        )
        rejection = {"detail": {"error": {"code": "plan_not_approvable"}}}
        await store.finalize_rejected_chat_submission(**common, lease_owner=claim.lease_owner, lease_version=claim.lease_version, response=rejection)
        retry_payload = copy.deepcopy(payload)
        retry_payload["input_document"]["nodes"][0]["label"] = "重命名分析师"
        retry_payload["message"] = InputDocument.model_validate(retry_payload["input_document"]).display_text()
        retry = await client.post("/api/v1/chat/send", content=json.dumps(retry_payload), headers=headers)
        assert retry.status_code == 409
        assert retry.json()["detail"]["error"]["code"] == "plan_not_approvable"
        lookup_url = f"/api/v1/chat/submissions/{payload['client_submission_id']}"
        lookup = await client.get(lookup_url, params={"workspace_id": workspace_id})
        assert lookup.status_code == 200 and lookup.json()["response"] == rejection
        retry_payload["input_document"]["nodes"][2]["parameters"]["period"] = "2026Q4"
        conflict = await client.post("/api/v1/chat/send", content=json.dumps(retry_payload), headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["error"]["code"] == "submission_id_conflict"
        # Current authorization must still run even if legacy dev fallbacks are enabled.
        await users.remove_member(workspace_id, user.id)
        monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
        denied = await client.post("/api/v1/chat/send", content=raw, headers=headers)
        assert denied.status_code == 403, denied.text
        lookup_denied = await client.get(lookup_url, params={"workspace_id": workspace_id})
        assert lookup_denied.status_code == 403, lookup_denied.text
        abandon_denied = await client.post(lookup_url + "/abandon", json={"workspace_id": workspace_id})
        assert abandon_denied.status_code == 403, abandon_denied.text
        assert await users.get_member_role(workspace_id, user.id) is None
        with sqlite3.connect(db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM chat_submission_inflight").fetchone()[0] == 0

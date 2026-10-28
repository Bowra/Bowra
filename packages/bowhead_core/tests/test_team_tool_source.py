"""team_tool_source data layer + Fernet vault + audit (PRD §8 / tech-design §7).

asyncio.run wrappers (no pytest-asyncio dep). Cross-team isolation + uniqueness +
auth round-trip (FERNET, not plaintext) + F14 plaintext-refusal guard.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from bowhead_core.team_tool_loader import load_team_tool_entries
from bowhead_core.user_store import TeamToolSource, UserStore, _id, _now
from cryptography.fernet import Fernet


def _run(coro):
    return asyncio.run(coro)


def _src(team_id="team_a", name="datarc", **kw):
    return TeamToolSource(
        id=_id("ots"),
        team_id=team_id,
        name=name,
        base_url="https://api.datarc.cn",
        created_at=_now(),
        updated_at=_now(),
        **kw,
    )


_SPEC_2OP = json.dumps(
    {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/a": {"get": {"operationId": "opA", "summary": "spec summary A", "responses": {"200": {"description": "ok"}}}},
            "/b": {"get": {"operationId": "opB", "summary": "spec summary B", "responses": {"200": {"description": "ok"}}}},
        },
    }
)


def test_cca_p2_tool_overrides_round_trip(tmp_path, monkeypatch):
    """Per-op description override persists through create → get (cca P2 slice 1)."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())

    async def go():
        s = UserStore(str(tmp_path / "u.db"))
        await s.initialize()
        src = _src(tool_overrides={"opA": {"description": "my clearer description"}})
        await s.create_team_tool_source(src)
        got = await s.get_team_tool_source("team_a", src.id)
        assert got is not None
        assert got.tool_overrides == {"opA": {"description": "my clearer description"}}

    _run(go())


def test_cca_p2_loader_overlays_description_override_onto_summary(tmp_path, monkeypatch):
    """LOAD POINT overlays the per-op override onto op.summary (the LLM-facing description); a
    non-overridden op keeps the spec summary. The registered NAME is derived = unaffected."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())

    async def go():
        s = UserStore(str(tmp_path / "u.db"))
        await s.initialize()
        src = _src(
            openapi_spec_json=_SPEC_2OP, selected_op_ids=["opA", "opB"], status="enabled", tool_overrides={"opA": {"description": "OVERRIDDEN A"}}
        )
        await s.create_team_tool_source(src)
        entries, by_id = await load_team_tool_entries("team_a", s)
        # tools sprint WP0: the loader returns normalized TeamToolEntry (not (sid, op) tuples); the
        # override is overlaid onto entry.description (the LLM-facing text), op carries the OpenApiOp.
        by_op = {e.op.operation_id: e for e in entries if e.op}
        assert by_op["opA"].description == "OVERRIDDEN A"  # override applied
        assert by_op["opB"].description == "spec summary B"  # no override → spec value
        assert src.id in by_id

    _run(go())


def test_create_get_list_and_cross_team_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())

    async def go():
        s = UserStore(str(tmp_path / "u.db"))
        await s.initialize()
        src = _src()
        await s.create_team_tool_source(src)
        got = await s.get_team_tool_source("team_a", src.id)
        assert got is not None and got.name == "datarc" and got.base_url == "https://api.datarc.cn"
        # cross-team: team_b cannot see team_a's source
        assert await s.get_team_tool_source("team_b", src.id) is None
        assert [x.id for x in await s.list_team_tool_sources("team_a")] == [src.id]
        assert await s.list_team_tool_sources("team_b") == []

    _run(go())


def test_unique_name_per_team(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())

    async def go():
        s = UserStore(str(tmp_path / "u.db"))
        await s.initialize()
        await s.create_team_tool_source(_src(name="dup"))
        with pytest.raises(ValueError):
            await s.create_team_tool_source(_src(name="dup"))
        # same name OK in a DIFFERENT team (per-turn view isolates at runtime)
        await s.create_team_tool_source(_src(team_id="team_b", name="dup"))

    _run(go())


def test_auth_encrypted_roundtrip_not_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())

    async def go():
        s = UserStore(str(tmp_path / "u.db"))
        await s.initialize()
        src = _src()
        await s.create_team_tool_source(src)
        await s.update_team_tool_source_auth(
            "team_a",
            src.id,
            auth_type="bearer",
            auth={"token": "sk-supersecret-123"},
        )
        # ciphertext at rest, NOT plaintext
        row = await s.get_team_tool_source("team_a", src.id)
        assert row is not None and row.auth_type == "bearer"
        assert row.auth_ref and row.auth_ref.startswith("FERNET:")
        assert "sk-supersecret-123" not in row.auth_ref
        # decrypt round-trip (team-bound)
        assert await s.get_team_tool_auth_decrypted("team_a", src.id) == {"token": "sk-supersecret-123"}
        # cross-team decrypt yields nothing
        assert await s.get_team_tool_auth_decrypted("team_b", src.id) == {}

    _run(go())


def test_f14_refuse_plaintext_when_no_fernet_key(tmp_path, monkeypatch):
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)

    async def go():
        s = UserStore(str(tmp_path / "u.db"))
        await s.initialize()
        src = _src()
        await s.create_team_tool_source(src)
        with pytest.raises(RuntimeError, match="FERNET_KEY"):
            await s.update_team_tool_source_auth(
                "team_a",
                src.id,
                auth_type="api_key",
                auth={"key": "secret"},
            )
        # auth_type='none' is fine without a key
        await s.update_team_tool_source_auth("team_a", src.id, auth_type="none")

    _run(go())


def test_delete_and_audit_log(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())

    async def go():
        s = UserStore(str(tmp_path / "u.db"))
        await s.initialize()
        src = _src()
        await s.create_team_tool_source(src)
        # audit write (no secret / no raw args)
        await s.log_tool_call(
            team_id="team_a",
            user_id="u1",
            source_id=src.id,
            tool_name="teammcp.x.get_metric",
            args_digest="sha256:abc",
            status="ok",
            duration_ms=42,
            upstream_status=200,
            bytes_out=128,
        )
        # delete is team-bound
        assert await s.delete_team_tool_source("team_b", src.id) is False
        assert await s.delete_team_tool_source("team_a", src.id) is True
        assert await s.get_team_tool_source("team_a", src.id) is None

    _run(go())


def test_prompt_cache_writer_guards_legacy_schema_and_argument_limit(tmp_path):
    """Direct Store callers cannot bypass cache bounds or break rolling upgrades."""

    async def go():
        store = UserStore(str(tmp_path / "u.db"))
        await store.initialize()  # legacy bootstrap intentionally has no 0093 column
        src = _src(kind="mcp", tools_schema=[{"name": "old"}])
        await store.create_team_tool_source(src)

        # During a rolling upgrade, an empty Prompt refresh is a no-op while
        # independent Tool metadata must still be saved.
        await store.update_team_tool_source_meta(
            "team_a",
            src.id,
            tools_schema=[{"name": "fresh"}],
            prompts_schema=[],
        )
        current = await store.get_team_tool_source("team_a", src.id)
        assert current is not None
        assert current.tools_schema == [{"name": "fresh"}]

        prompt = {
            "name": "bounded",
            "title": "",
            "description": "",
            "arguments": [],
        }
        with pytest.raises(RuntimeError, match="schema_unavailable"):
            await store.update_team_tool_source_meta(
                "team_a",
                src.id,
                prompts_schema=[prompt],
            )

        invalid = dict(prompt)
        invalid["arguments"] = [{"name": f"arg_{index}", "description": "", "required": False} for index in range(129)]
        with pytest.raises(ValueError, match="invalid MCP Prompt metadata"):
            await store.update_team_tool_source_meta(
                "team_a",
                src.id,
                prompts_schema=[invalid],
            )

    _run(go())


def test_prompt_cache_writer_rejects_non_mcp_sources(tmp_path):
    async def go():
        store = UserStore(str(tmp_path / "u.db"))
        await store.initialize()
        with sqlite3.connect(store.db_path) as db:
            db.execute(
                "ALTER TABLE team_tool_source ADD COLUMN prompts_schema_json TEXT NOT NULL DEFAULT '[]'",
            )
        prompt = {
            "name": "profitability",
            "title": "",
            "description": "",
            "arguments": [],
        }

        with pytest.raises(ValueError, match="requires an MCP source"):
            await store.create_team_tool_source(
                _src(kind="openapi", prompts_schema=[prompt]),
            )

        src = _src(kind="openapi")
        await store.create_team_tool_source(src)
        with pytest.raises(ValueError, match="requires an MCP source"):
            await store.update_team_tool_source_meta(
                "team_a",
                src.id,
                prompts_schema=[prompt],
            )

    _run(go())

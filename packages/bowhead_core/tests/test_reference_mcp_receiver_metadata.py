from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_behavior_materializer import default_agent_behavior_materializer
from bowhead_core.reference_receiver_metadata import (
    ReferenceReceiverMetadata,
    ReferenceTeamToolBaseline,
    build_reference_receiver_metadata,
    published_team_tool_baseline,
)
from test_agent_admission_transaction import _team_admission_case
from test_agent_execution_binding import _insert_release


def _dependency(*tools, fingerprint: str = "sha256:release") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "compiler_version": "agent-dependency-v1",
            "fingerprint": fingerprint,
            "status": "review_required",
            "tools": list(tools),
        }
    )


def _tool(name: str, *, source_id: str = "ots_finance", risk: str = "read", revision: str = "2026-09-16T00:00:00+00:00") -> dict:
    return {
        "approval_required": risk != "read",
        "capability_classes": ["data_query"],
        "connection_ref": f"team_tool_source:{source_id}",
        "method": "GET" if risk == "read" else "POST",
        "operation_id": name.rsplit(".", 1)[-1],
        "revision": revision,
        "risk": risk,
        "risk_source": "openapi_method",
        "source_kind": "team_mcp",
        "stable_ref": name,
    }


def test_published_team_tool_baseline_keeps_exact_release_identity():
    first = "teammcp.ots_finance.getMargin"
    second = "teammcp.ots_finance.setMargin"
    native = {**_tool("bi_query"), "source_kind": "native", "connection_ref": None}
    result = published_team_tool_baseline(
        {"tool_refs": [first, second, "bi_query"]},
        _dependency(native, _tool(second, risk="external_write"), _tool(first)),
        expected_fingerprint="sha256:release",
        projection_compiler="agent-dependency-v1",
    )
    assert [item.operation_name for item in result] == [first, second]
    assert result[0] == ReferenceTeamToolBaseline(
        source_id="ots_finance",
        operation_name=first,
        source_revision="2026-09-16T00:00:00+00:00",
        risk="read",
        approval_required=False,
    )
    receiver = ReferenceReceiverMetadata((), team_tools=result)
    assert receiver.has_team_tool_baseline(
        source_id="ots_finance",
        operation_name=first,
        source_revision="2026-09-16T00:00:00+00:00",
    )
    assert not receiver.has_team_tool_baseline(source_id="ots_finance", operation_name=first, source_revision="changed")


@pytest.mark.parametrize(
    "definition,dependency,fingerprint,compiler",
    [
        (
            {"tool_refs": ["teammcp.ots_finance.getMargin"]},
            _dependency(_tool("teammcp.ots_finance.getMargin")),
            "sha256:other",
            "agent-dependency-v1",
        ),
        ({"tool_refs": ["teammcp.ots_finance.getMargin"]}, _dependency(_tool("teammcp.ots_finance.other")), "sha256:release", "agent-dependency-v1"),
        (
            {"tool_refs": ["teammcp.ots_finance.getMargin"]},
            _dependency(_tool("teammcp.ots_other.getMargin")),
            "sha256:release",
            "agent-dependency-v1",
        ),
        (
            {"tool_refs": ["teammcp.ots_finance.getMargin"]},
            _dependency(_tool("teammcp.ots_finance.getMargin"), _tool("teammcp.ots_finance.getMargin")),
            "sha256:release",
            "agent-dependency-v1",
        ),
        (
            {"tool_refs": ["teammcp.ots_finance.getMargin"], "tool_refs_json": "[]"},
            _dependency(_tool("teammcp.ots_finance.getMargin")),
            "sha256:release",
            "agent-dependency-v1",
        ),
        ({"tool_refs": ["teammcp.ots_finance.getMargin"]}, _dependency(_tool("teammcp.ots_finance.getMargin")), "sha256:release", "other"),
    ],
)
def test_published_team_tool_baseline_fails_closed(definition, dependency, fingerprint, compiler):
    assert (
        published_team_tool_baseline(
            definition,
            dependency,
            expected_fingerprint=fingerprint,
            projection_compiler=compiler,
        )
        == ()
    )


def test_receiver_rejects_ambiguous_team_tool_baseline():
    item = ReferenceTeamToolBaseline(
        source_id="ots_finance",
        operation_name="teammcp.ots_finance.getMargin",
        source_revision="2026-09-16T00:00:00+00:00",
        risk="read",
        approval_required=False,
    )
    with pytest.raises(ValueError, match="invalid receiver Team Tool baseline"):
        ReferenceReceiverMetadata((), team_tools=(item, item))


@pytest.mark.asyncio
async def test_receiver_cache_reads_only_matching_live_dependency_projection(tmp_path):
    case = await _team_admission_case(tmp_path)
    stable_ref = "teammcp.ots_finance.getMargin"
    await _insert_release(
        case.fixture,
        sequence=2,
        prompt="revision two",
        definition_overrides={"tool_refs": [stable_ref]},
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        db.row_factory = sqlite3.Row
        revision = db.execute("SELECT * FROM team_subagent_revision WHERE id='arv_exact_2'").fetchone()
    materializer = default_agent_behavior_materializer()
    descriptor = materializer.compile_legacy_team_revision(
        revision_id=revision["id"],
        definition=json.loads(revision["definition_json"]),
        source_definition_hash=revision["definition_hash"],
        dependency_fingerprint=revision["dependency_fingerprint"],
        capability_fingerprint=revision["capability_fingerprint"],
    )
    async with open_shared_ctx(case.fixture.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        await materializer.persist_team_revision_conn(
            db,
            revision_id=revision["id"],
            team_id=case.fixture.team_id,
            subagent_id="osa_exact",
            descriptor=descriptor,
            created_at=datetime.now(UTC).isoformat(),
        )
        await db.execute(
            """INSERT INTO agent_catalog_projection(
                 team_id,agent_id,source_kind,source_version,compiler_version,canonical_name,
                 canonical_summary,owner_label,capability_summary_json,facet_json,risk_summary_json,
                 safety_summary_json,dependency_json,search_text,projection_hash,built_at
               ) VALUES (?,?,'team',?,?,'exact-agent','summary',NULL,'[]','{}','{}','{}',?,'exact agent',?,?)""",
            (
                case.fixture.team_id,
                "osa_exact",
                revision["id"],
                "agent-dependency-v1",
                _dependency(_tool(stable_ref), fingerprint=revision["dependency_fingerprint"]),
                "sha256:projection",
                datetime.now(UTC).isoformat(),
            ),
        )
        await db.commit()
    index = await build_reference_receiver_metadata(case.fixture.db_path)
    receiver = index.items[0].receiver
    assert receiver.has_team_tool_baseline(
        source_id="ots_finance",
        operation_name=stable_ref,
        source_revision="2026-09-16T00:00:00+00:00",
    )

    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE agent_catalog_projection SET source_version='arv_stale'")
    stale = await build_reference_receiver_metadata(case.fixture.db_path)
    assert stale.items[0].receiver.team_tools == ()

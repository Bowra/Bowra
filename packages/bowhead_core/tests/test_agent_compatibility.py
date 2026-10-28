from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.agent_compatibility import (
    AgentCompatibilityCompiler,
    AgentCompatibilityStore,
)

from bowhead_core import migrations

pytestmark = pytest.mark.integration
NOW = "2026-09-14T09:00:00+00:00"


def _definition(*, include_name: bool = True, primary: str | None = "cap_report@1") -> dict[str, object]:
    properties: dict[str, object] = {"value": {"type": "number"}}
    required = ["value"]
    if include_name:
        properties["name"] = {"type": "string"}
        required.append("name")
    return {
        "output_schema": {
            "additionalProperties": False,
            "properties": properties,
            "required": required,
            "type": "object",
        },
        "primary_capability_ref": primary,
        "required_delivery_formats": ["report"],
        "system_prompt": "Implementation text is not a published contract",
    }


def test_compiler_keeps_compatible_major_and_ignores_prompt_text() -> None:
    compiler = AgentCompatibilityCompiler()
    base = _definition()
    candidate = {
        **base,
        "output_schema": {
            **base["output_schema"],
            "properties": {
                **base["output_schema"]["properties"],
                "optional_note": {"type": "string"},
            },
        },
        "system_prompt": "Completely rewritten implementation text",
    }

    decision = compiler.compile(
        revision_id="arv_candidate",
        candidate_definition=candidate,
        base_revision_id="arv_base",
        base_definition=base,
        base_compatibility_major=4,
    )

    assert decision.verdict == "compatible"
    assert decision.compatibility_major == 4
    assert decision.reason_codes == ("published_contract_compatible",)


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (_definition(include_name=False), "output_schema_field_removed"),
        (
            {
                **_definition(),
                "output_schema": {
                    **_definition()["output_schema"],
                    "properties": {
                        **_definition()["output_schema"]["properties"],
                        "value": {"type": "integer"},
                    },
                },
            },
            "output_schema_type_narrowed",
        ),
        (_definition(primary=None), "primary_capability_removed_or_changed"),
    ],
)
def test_compiler_increments_major_for_proven_breaks(
    candidate: dict[str, object],
    reason: str,
) -> None:
    decision = AgentCompatibilityCompiler().compile(
        revision_id="arv_candidate",
        candidate_definition=candidate,
        base_revision_id="arv_base",
        base_definition=_definition(),
        base_compatibility_major=2,
    )

    assert decision.verdict == "breaking"
    assert decision.compatibility_major == 3
    assert reason in decision.reason_codes


def test_compiler_does_not_claim_compatibility_for_changed_complex_schema() -> None:
    base = _definition()
    candidate = {
        **base,
        "output_schema": {
            **base["output_schema"],
            "oneOf": [{"required": ["name"]}, {"required": ["value"]}],
        },
    }

    decision = AgentCompatibilityCompiler().compile(
        revision_id="arv_candidate",
        candidate_definition=candidate,
        base_revision_id="arv_base",
        base_definition=base,
        base_compatibility_major=2,
    )

    assert decision.verdict == "unknown"
    assert decision.compatibility_major == 2
    assert "output_schema_complex_constraint_changed" in decision.reason_codes


@pytest.mark.asyncio
async def test_store_uses_exact_base_major_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO teams(id,name,status,created_at) VALUES('team_c','Team','active',?)", (NOW,))
        db.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,created_by,status,definition_mode,created_at,updated_at
            ) VALUES(
              'osa_c','team_c','compat-agent','Compat Agent','','[]','[]',
              'private','user_owner','enabled','revisioned',?,?
            )
            """,
            (NOW, NOW),
        )
        for sequence, revision_id, definition in (
            (1, "arv_c_1", _definition()),
            (2, "arv_c_2", {**_definition(), "system_prompt": "changed"}),
            (3, "arv_c_3", _definition(include_name=False)),
        ):
            payload = json.dumps(definition, separators=(",", ":"), sort_keys=True)
            db.execute(
                """
                INSERT INTO team_subagent_revision(
                  id,team_id,subagent_id,sequence,revision_kind,definition_json,
                  listing_json,definition_hash,dependency_fingerprint,
                  capability_fingerprint,risk_compiler_version,risk_json,
                  created_by,created_at
                ) VALUES(?,?,?,?,? ,?,'{}','sha256:def','sha256:dep','sha256:cap',
                         'risk-v1','{}','user_owner',?)
                """,
                (revision_id, "team_c", "osa_c", sequence, "authored", payload, NOW),
            )

    store = AgentCompatibilityStore(db_path)
    initial = await store.compile_and_store(
        team_id="team_c",
        agent_id="osa_c",
        revision_id="arv_c_1",
        base_revision_id=None,
        created_at=NOW,
    )
    compatible = await store.compile_and_store(
        team_id="team_c",
        agent_id="osa_c",
        revision_id="arv_c_2",
        base_revision_id="arv_c_1",
        created_at=NOW,
    )
    duplicate = await store.compile_and_store(
        team_id="team_c",
        agent_id="osa_c",
        revision_id="arv_c_2",
        base_revision_id="arv_c_1",
        created_at=NOW,
    )
    breaking = await store.compile_and_store(
        team_id="team_c",
        agent_id="osa_c",
        revision_id="arv_c_3",
        base_revision_id="arv_c_2",
        created_at=NOW,
    )

    assert initial.compatibility_major == compatible.compatibility_major == 1
    assert duplicate == compatible
    assert breaking.verdict == "breaking"
    assert breaking.compatibility_major == 2
    assert await store.get(team_id="team_c", agent_id="osa_c", revision_id="arv_c_3") == breaking


def _upgrade(path: Path) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")

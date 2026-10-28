from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_dependency_compiler import (
    AGENT_DEPENDENCY_COMPILER_VERSION,
    AgentDependencyCompilation,
    AgentRiskAssessment,
    ExactSkillRuntimeScope,
)
from bowhead_core.agent_eval_product import AgentEvalReleaseGate
from bowhead_core.agent_release_outbox import (
    AgentReleaseNotificationOutbox,
    AgentReleaseOutboxForbiddenError,
)
from bowhead_core.agent_release_read_store import AgentReleaseReadStore, _audience_expanded, _changed_keys
from bowhead_core.agent_revision_store import (
    AgentAudience,
    AgentPublicationBlockedError,
    AgentPublicationValidation,
    AgentReleaseStaleError,
    AgentRevisionConflictError,
    AgentRevisionForbiddenError,
    AgentRevisionStore,
    AgentSemanticSafetyResult,
    CandidateReplacement,
    DeterministicAgentPublicationValidator,
)
from bowhead_core.agent_safety_check_service import (
    AgentSafetyCheckService,
    PersistedAgentSemanticEvidenceResolver,
)
from bowhead_core.audit_actions import AuditAction
from bowhead_core.catalog_localization import (
    CatalogLocalizationStore,
    agent_listing_source_hash,
    materialize_confirmed_agent_localizations_conn,
)
from bowhead_core.notification_store import NotificationStore
from bowhead_core.team_subagent_store import (
    TeamSubagentRevisionWriterRequiredError,
    TeamSubagentStore,
    effective_agent_name_key,
)
from bowhead_core.user_store import TeamSubagent
from legacy_agent_release_fixture import seed_legacy_publication

_TIME = "2026-09-03T08:00:00+00:00"


def test_audience_expansion_distinguishes_growth_from_contraction() -> None:
    custom = {
        "effective_mode": "custom",
        "user_ids": ["user_a", "user_b"],
        "group_ids": ["group_a"],
    }

    assert not _audience_expanded(
        custom,
        {"effective_mode": "custom", "user_ids": ["user_a"], "group_ids": []},
    )
    assert _audience_expanded(
        custom,
        {
            "effective_mode": "custom",
            "user_ids": ["user_a", "user_b", "user_c"],
            "group_ids": ["group_a"],
        },
    )
    assert not _audience_expanded(custom, {"effective_mode": "private"})
    assert _audience_expanded(custom, {"effective_mode": "teamwide"})


def test_reviewer_listing_diff_hides_derived_keys_but_never_the_translations() -> None:
    """A reviewer diff answers "what did the Owner change".

    `source_locale` and `localization_metadata` are written by the localization
    materializer on every publish, so reporting them would put a listing edit on the
    first post-M4 revision of every Agent that nobody made.  The translations
    themselves are the opposite case: consumer-facing copy the Owner confirmed, and a
    reviewer approving the candidate has to see them move.
    """
    base = {"display_name": "Finance Assistant", "summary": "A useful specialist."}
    materialized = {
        **base,
        "source_locale": "zh-CN",
        "localization_metadata": {"en-US": {"source_hash": "sha256:abc", "generation_source": "llm"}},
        "localized_listings": {"en-US": {"display_name": "Finance Assistant", "summary": "A useful specialist."}},
        "localized_display_names": {"en-US": "Finance Assistant"},
    }

    # Adding translations to an otherwise untouched listing is a reviewable change...
    assert _changed_keys(base, materialized) == ["localized_display_names", "localized_listings"]

    # ...while the materializer's own bookkeeping is not.
    assert _changed_keys(base, {**base, "source_locale": "zh-CN"}) == []
    assert _changed_keys({**base, "localization_metadata": {}}, materialized) == [
        "localized_display_names",
        "localized_listings",
    ]


@dataclass
class _Clock:
    value: datetime = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class _Validator:
    def __init__(
        self,
        *,
        safety_status: str = "pass",
        risk_level: str = "low",
        token: str = "v1",
    ) -> None:
        self.safety_status = safety_status
        self.risk_level = risk_level
        self.token = token

    async def validate(self, _db, **_kwargs) -> AgentPublicationValidation:
        compilation = _compilation(token=self.token, risk_level=self.risk_level)
        semantic_reasons = () if self.safety_status == "pass" else ("semantic_scanner_unavailable",)
        return AgentPublicationValidation(
            compilation=compilation,
            safety_status=self.safety_status,
            deterministic_reason_codes=(),
            semantic_status="pass" if self.safety_status == "pass" else "not_run",
            semantic_reason_codes=semantic_reasons,
        )


def _candidate_hash_of(db_path, candidate_id: str) -> str:
    with sqlite3.connect(db_path) as connection:
        return str(
            connection.execute(
                "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
                (candidate_id,),
            ).fetchone()[0],
        )


class _EvalGate:
    def __init__(self) -> None:
        self.candidate_ids: list[str] = []
        self.prepared: list[str] = []

    async def prepare_for_activation(self, _db_path, *, team_id, agent_id, actor_user_id):
        self.prepared.append(f"{team_id}/{agent_id}/{actor_user_id}")

    async def evidence_for_activation(self, _db, *, candidate, audience):
        assert audience.mode != "private"
        self.candidate_ids.append(str(candidate["id"]))
        return {
            "kind": "agent_eval_release_evidence",
            "schema_version": 1,
            "runs": [{"run_id": "aer_release_contract"}],
        }


def _compilation(*, token: str = "v1", risk_level: str = "low") -> AgentDependencyCompilation:
    risk = AgentRiskAssessment(
        level=risk_level,
        data="none",
        effect="external_write" if risk_level == "high" else "read_only",
        automation="interactive",
        scope="team_resource",
        reversibility="compensatable" if risk_level == "high" else "none",
        reasons=(("external_write",) if risk_level == "high" else ()),
        unknown_dependencies=(),
        approval_required=risk_level == "high",
        runtime_confirmation_required=risk_level == "high",
    )
    dependency = _sha(f"dependency:{token}")
    return AgentDependencyCompilation(
        status="ready",
        reason_codes=(),
        dependency_fingerprint=dependency,
        capability_fingerprint=_sha(f"capability:{token}"),
        tools=(),
        skills=(),
        skill_scope=ExactSkillRuntimeScope(read_names=(), invoke_names=()),
        risk=risk,
        compiler_version=AGENT_DEPENDENCY_COMPILER_VERSION,
    )


class _CatalogService:
    def __init__(self) -> None:
        self.entitlements: list[bool] = []

    async def build(self, **kwargs):
        self.entitlements.append(kwargs["mcp_entitled"])
        return object()


class _DependencyCompiler:
    def __init__(self) -> None:
        self.coverages: list[frozenset[str]] = []

    async def compile(self, _definition, *, context, tool_catalog):
        del tool_catalog
        self.coverages.append(context.reference_coverage)
        return _compilation()


class _McpResolver:
    entitled = False

    async def is_entitled(self, _db, **_kwargs) -> bool:
        return self.entitled


class _CoverageResolver:
    coverage = frozenset[str]()

    async def resolve(self, _db, **_kwargs) -> frozenset[str]:
        return self.coverage


class _SemanticScanner:
    scanner_version = "test-semantic-v1"

    def __init__(self, status: str, reasons: tuple[str, ...]) -> None:
        self.status = status
        self.reasons = reasons
        self.calls = 0

    async def scan(self, **_kwargs) -> AgentSemanticSafetyResult:
        self.calls += 1
        return AgentSemanticSafetyResult(
            status=self.status,  # type: ignore[arg-type]
            reason_codes=self.reasons,
            scanner_version=self.scanner_version,
        )


@pytest.mark.asyncio
async def test_deterministic_validator_reloads_entitlement_and_coverage() -> None:
    catalog_service = _CatalogService()
    compiler = _DependencyCompiler()
    entitlement = _McpResolver()
    coverage = _CoverageResolver()
    validator = DeterministicAgentPublicationValidator(
        catalog_store=object(),
        dependency_compiler=compiler,  # type: ignore[arg-type]
        catalog_service=catalog_service,  # type: ignore[arg-type]
        mcp_entitlement_resolver=entitlement,
        reference_coverage_resolver=coverage,
    )
    async with aiosqlite.connect(":memory:") as db:
        await validator.validate(
            db,
            team_id="team_release",
            agent_id="osa_release",
            owner_user_id="user_owner",
            routing_name="Release Agent",
            definition=_definition(),
            listing=_listing(),
            audience=AgentAudience("custom", user_ids=("user_member",)),
        )
        entitlement.entitled = True
        coverage.coverage = frozenset({f"team:skill_one@{_sha('skill-one')}"})
        await validator.validate(
            db,
            team_id="team_release",
            agent_id="osa_release",
            owner_user_id="user_owner",
            routing_name="Release Agent",
            definition=_definition(),
            listing=_listing(),
            audience=AgentAudience("custom", user_ids=("user_member",)),
        )
    assert catalog_service.entitlements == [False, True]
    assert compiler.coverages == [frozenset(), coverage.coverage]


@pytest.mark.asyncio
async def test_deterministic_validator_blocks_prompt_secret_without_echoing_it() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    validator = DeterministicAgentPublicationValidator(
        catalog_store=object(),
        dependency_compiler=_DependencyCompiler(),  # type: ignore[arg-type]
        catalog_service=_CatalogService(),  # type: ignore[arg-type]
    )
    async with aiosqlite.connect(":memory:") as db:
        result = await validator.validate(
            db,
            team_id="team_release",
            agent_id="osa_release",
            owner_user_id="user_owner",
            routing_name="release-agent",
            definition=_definition(f"API_TOKEN={secret}"),
            listing={
                **_listing(),
                "localized": {"en-US": {"tagline": "Safe text"}},
            },
            audience=AgentAudience("private"),
        )
    assert result.safety_status == "blocked"
    assert "credential_or_secret" in result.deterministic_reason_codes
    assert secret not in repr(result)

    async with aiosqlite.connect(":memory:") as db:
        nested_listing_result = await validator.validate(
            db,
            team_id="team_release",
            agent_id="osa_release",
            owner_user_id="user_owner",
            routing_name="release-agent",
            definition=_definition(),
            listing={
                **_listing(),
                "localized": {"en-US": {"tagline": f"API_TOKEN={secret}"}},
            },
            audience=AgentAudience("private"),
        )
    assert nested_listing_result.safety_status == "blocked"
    assert "credential_or_secret" in nested_listing_result.deterministic_reason_codes
    assert secret not in repr(nested_listing_result)


@pytest.mark.asyncio
async def test_deterministic_validator_blocks_unproven_primary_capability() -> None:
    validator = DeterministicAgentPublicationValidator(
        catalog_store=object(),
        dependency_compiler=_DependencyCompiler(),  # type: ignore[arg-type]
        catalog_service=_CatalogService(),  # type: ignore[arg-type]
    )
    definition = {
        **_definition(),
        "primary_capability_ref": "cap_analysis@1",
    }

    async with aiosqlite.connect(":memory:") as db:
        result = await validator.validate(
            db,
            team_id="team_release",
            agent_id="osa_release",
            owner_user_id="user_owner",
            routing_name="release-agent",
            definition=definition,
            listing=_listing(),
            audience=AgentAudience("private"),
        )

    assert result.safety_status == "blocked"
    assert result.behavior_primary_requested is True
    assert result.behavior_status == "blocked"
    assert result.behavior_fingerprint is not None
    assert "primary_capability_unproven" in result.deterministic_reason_codes
    assert result.safe_summary_dict()["reason_codes"] == [
        "behavior_compile_blocked",
        "primary_capability_unproven",
    ]


def _sha(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _db(tmp_path):
    db_path = tmp_path / "bowhead.db"
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    return db_path


def _definition(prompt: str = "Be helpful.") -> dict:
    return {
        "max_iterations": 10,
        "model": None,
        "output_schema": None,
        "skill_grants": [],
        "system_prompt": prompt,
        "tool_refs": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_format", [False, True])
async def test_working_copy_rejects_new_approval_tools_but_preserves_existing_bindings(tmp_path, monkeypatch, legacy_format):
    from bowhead_core.openapi_tool_source import op_tool_name
    from bowhead_core.user_store import TeamToolSource, UserStore

    from bowhead_core import approval_gate

    db_path = _db(tmp_path)
    _seed(db_path)
    source_id = "ots_approval"
    ref = op_tool_name(source_id, "update")
    new_ref = op_tool_name(source_id, "delete")
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "CRM", "version": "1"},
        "paths": {
            "/crm": {
                method: {"operationId": operation, "responses": {"200": {"description": "ok"}}}
                for method, operation in (("post", "update"), ("delete", "delete"))
            }
        },
    }
    await UserStore(str(db_path)).create_team_tool_source(
        TeamToolSource(
            id=source_id,
            team_id="team_release",
            name="CRM",
            base_url="https://93.184.216.34",
            openapi_spec_json=json.dumps(spec),
            selected_op_ids=["update", "delete"],
            created_at=_TIME,
            updated_at=_TIME,
        )
    )
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset({ref, new_ref}))
    monkeypatch.setenv("BOWHEAD_APPROVAL_WRITE_ENABLED", "1")
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())
    definition = {**_definition(), "tool_refs": ["bi_query", ref]}

    async def save(definition, version, description="A useful specialist."):
        return await store.save_working_copy(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            definition=definition,
            listing={**_listing(), "description": description},
            audience=AgentAudience("private"),
            expected_row_version=version,
        )

    legacy_definition = {key: value for key, value in definition.items() if key != "tool_refs"}
    legacy_definition["tool_refs_json"] = json.dumps(definition["tool_refs"])
    initial = await save(legacy_definition if legacy_format else definition, None)
    monkeypatch.setenv("BOWHEAD_APPROVAL_WRITE_ENABLED", "0")
    retained = await save(definition, initial.row_version, "Description only")
    assert retained.definition["tool_refs"] == ["bi_query", ref]
    assert retained.listing["description"] == "Description only"
    with pytest.raises(AgentPublicationBlockedError, match="approval_executor_unavailable"):
        await save({**definition, "tool_refs": ["bi_query", ref, new_ref]}, retained.row_version)
    with pytest.raises(AgentPublicationBlockedError, match="approval_executor_unavailable"):
        await save({**legacy_definition, "tool_refs_json": json.dumps(["bi_query", ref, new_ref])}, retained.row_version)
    removed = await save({**definition, "tool_refs": ["bi_query"]}, retained.row_version)
    assert removed.definition["tool_refs"] == ["bi_query"]
    with pytest.raises(AgentPublicationBlockedError, match="approval_executor_unavailable"):
        await save(definition, removed.row_version)


def _listing(name: str = "New Agent") -> dict:
    return {"description": "A useful specialist.", "display_name": name}


def _seed(db_path, *, with_assignment: bool = True) -> None:
    definition = _definition("Old prompt.")
    listing = _listing("Old Agent")
    audience = {
        "effective_mode": "private",
        "group_ids": [],
        "schema_version": 1,
        "user_ids": [],
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO teams(id,name,status,created_by_user_id,created_at)
            VALUES ('team_release','Team','active','user_admin',?)
            """,
            (_TIME,),
        )
        connection.executemany(
            "INSERT INTO users(id,team_id,email,status,created_at) VALUES (?,?,?,?,?)",
            (
                ("user_owner", "team_release", "owner@example.com", "active", _TIME),
                ("user_admin", "team_release", "admin@example.com", "active", _TIME),
                ("user_member", "team_release", "member@example.com", "active", _TIME),
            ),
        )
        connection.executemany(
            """
            INSERT INTO team_members(id,user_id,team_id,role,status,created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                ("tm_owner", "user_owner", "team_release", "member", "active", _TIME),
                ("tm_admin", "user_admin", "team_release", "team_admin", "active", _TIME),
                ("tm_member", "user_member", "team_release", "member", "active", _TIME),
            ),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,system_prompt,tool_refs_json,
              model,max_iterations,skills_json,output_schema_json,visibility,created_by,
              status,created_at,updated_at,owner_user_id,row_version,audience_mode,
              lifecycle_state,live_revision_id,live_release_id,release_sequence,
              definition_mode
            ) VALUES (
              'osa_release','team_release','release-agent','Old Agent','Old listing',
              'Old prompt.','[]',NULL,10,'[]',NULL,'creator_only','user_owner','enabled',
              ?,?,'user_owner',1,'private','active',NULL,NULL,0,'legacy_mutable'
            )
            """,
            (_TIME, _TIME),
        )
        connection.execute(
            """
            INSERT INTO agent_name_claim(
              team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
            ) VALUES ('team_release','*',?,'agent','osa_release','osa_release',?)
            """,
            (effective_agent_name_key("Old Agent"), _TIME),
        )
        connection.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,status,
              deterministic_json,semantic_json,safe_summary_json,created_by,created_at
            ) VALUES (
              'asr_initial','team_release','osa_release','revision','arv_initial',?,?,
              'seed','seed','pass','{}','{}','{}','user_owner',?
            )
            """,
            (_sha("initial-content"), _sha("initial-dependency"), _TIME),
        )
        definition_json = _canonical(definition)
        listing_json = _canonical(listing)
        connection.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
              definition_hash,dependency_fingerprint,capability_fingerprint,
              risk_compiler_version,risk_json,created_by,created_at
            ) VALUES (
              'arv_initial','team_release','osa_release',1,'imported_baseline',?,?,?,?,?,
              'seed','{}','user_owner',?
            )
            """,
            (
                definition_json,
                listing_json,
                _sha(definition_json),
                _sha("initial-dependency"),
                _sha("initial-capability"),
                _TIME,
            ),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,
              audience_snapshot_json,audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_json,safety_report_id,activated_by,activated_at
            ) VALUES (
              'arl_initial','team_release','osa_release',1,'arv_initial',?,?,?,?,?,
              'asr_initial','user_owner',?
            )
            """,
            (
                _canonical(audience),
                _sha(_canonical(audience)),
                _sha("initial-capability"),
                _sha("initial-dependency"),
                "{}",
                _TIME,
            ),
        )
        connection.execute(
            """
            UPDATE team_subagent
            SET live_revision_id='arv_initial',live_release_id='arl_initial',
                release_sequence=1,definition_mode='revisioned'
            WHERE id='osa_release'
            """,
        )
        if with_assignment:
            connection.execute(
                """
                INSERT INTO agent_assignment(
                  id,team_id,user_id,agent_id,agent_source_kind,personal_state,access_state,
                  source,row_version,hired_at,current_cycle_started_at,
                  personal_state_changed_at,access_state_changed_at,created_at,updated_at
                ) VALUES (
                  'asa_release','team_release','user_member','osa_release','team','active',
                  'granted','user_hired',1,?,?,?,?,?,?
                )
                """,
                (_TIME, _TIME, _TIME, _TIME, _TIME, _TIME),
            )
        connection.commit()


def _seed_eval_workspace(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO workspaces(id,team_id,name,status,revision,created_at,updated_at)
            VALUES('ws_eval','team_release','Evaluation','active',1,?,?)
            """,
            (_TIME, _TIME),
        )
        connection.execute(
            """
            INSERT INTO workspace_members(
              workspace_id,user_id,role,invited_by,joined_at,last_active_at,status
            ) VALUES('ws_eval','user_owner','admin','user_owner',?,?,'active')
            """,
            (_TIME, _TIME),
        )


@pytest.mark.asyncio
async def test_prepare_working_copy_is_atomic_exact_and_idempotent(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(),
        eval_gate=AgentEvalReleaseGate(),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Candidate prompt."),
        listing=_listing("Candidate Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )

    with pytest.raises(AgentRevisionForbiddenError, match="agent_eval_workspace_execute_denied"):
        await store.prepare_working_copy(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=working.id,
            expected_working_copy_version=working.row_version,
            expected_eval_policy_revision_id=None,
            workspace_id="ws_missing",
            idempotency_key="prepare-invalid-workspace",
            release_note="Candidate validation",
        )
    with sqlite3.connect(db_path) as connection:
        for table in (
            "team_subagent_release_candidate",
            "agent_revision_compatibility",
            "agent_eval_policy_revision",
            "agent_eval_plan",
            "agent_eval_candidate_control",
            "agent_version_notification_outbox",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_revision").fetchone() == (1,)

    prepared = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        idempotency_key="prepare-success",
        release_note="Candidate validation",
    )
    replay = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        idempotency_key="prepare-success",
        release_note="Candidate validation",
    )

    assert replay == prepared
    assert prepared.state == "prepared"
    assert prepared.compatibility_major == 1
    assert prepared.compatibility_verdict == "compatible"
    assert prepared.control_row_version == 1
    assert prepared.candidate_hash.startswith("sha256:")
    assert prepared.plan_hash.startswith("sha256:")
    assert prepared.policy_hash.startswith("sha256:")
    assert prepared.source_working_copy_hash.startswith("sha256:")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        candidate = connection.execute(
            "SELECT * FROM team_subagent_release_candidate WHERE id=?",
            (prepared.candidate_id,),
        ).fetchone()
        control = connection.execute(
            "SELECT * FROM agent_eval_candidate_control WHERE candidate_id=?",
            (prepared.candidate_id,),
        ).fetchone()
        plan = connection.execute(
            "SELECT * FROM agent_eval_plan WHERE id=?",
            (prepared.plan_id,),
        ).fetchone()
        assert candidate is not None and candidate["state"] == "prepared"
        assert candidate["candidate_hash"] == prepared.candidate_hash
        assert control is not None and control["active_plan_id"] == prepared.plan_id
        assert control["source_working_copy_hash"] == prepared.source_working_copy_hash
        assert plan is not None and plan["candidate_hash"] == prepared.candidate_hash
        assert plan["policy_revision_id"] == prepared.policy_revision_id
        assert connection.execute("SELECT COUNT(*) FROM agent_revision_compatibility").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM agent_eval_policy_revision").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_eval_plan").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_eval_plan_suite").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_version_notification_outbox").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM admin_audit_log WHERE action=?",
                (AuditAction.AGENT_RELEASE_CANDIDATE_PREPARED,),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT live_release_id FROM team_subagent WHERE id='osa_release'").fetchone()[0] == "arl_initial"


class _RebasingValidator(_Validator):
    """Resolves one Skill whose current approved revision is newer than the definition's grant."""

    def __init__(self, current_revision: str) -> None:
        super().__init__()
        self.current_revision = current_revision
        self.definitions: list[dict] = []

    async def validate(self, _db, **kwargs) -> AgentPublicationValidation:
        from bowhead_core.agent_dependency_compiler import ExactSkillGrant, ResolvedSkillDependency

        self.definitions.append(kwargs["definition"])
        validation = await super().validate(_db, **kwargs)
        grant = ExactSkillGrant(
            kind="team",
            id="oskill_sales",
            name="sales-report",
            permissions=("read",),
            revision=self.current_revision,
            distribution="reference",
        )
        skill = ResolvedSkillDependency(
            grant=grant,
            kind="knowledge",
            has_scripts=False,
            risk="read",
            risk_reasons=(),
            risk_evidence_digest=None,
            review_required=False,
            available=True,
            reason_codes=(),
        )
        return dataclasses.replace(validation, compilation=dataclasses.replace(validation.compilation, skills=(skill,)))


@pytest.mark.asyncio
async def test_prepare_freezes_skill_grants_rebased_to_the_current_approved_revision(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    released, current = _sha("skill:v1"), _sha("skill:v2")
    validator = _RebasingValidator(current)
    store = AgentRevisionStore(str(db_path), validator=validator, eval_gate=AgentEvalReleaseGate(), clock=_Clock())
    grant = {
        "distribution": "reference",
        "permissions": ["read"],
        "revision": released,
        "skill_ref": {"id": "oskill_sales", "kind": "team", "name": "sales-report"},
    }
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition={**_definition("Candidate prompt."), "skill_grants": [grant]},
        listing=_listing("Candidate Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )

    prepared = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        idempotency_key="prepare-rebased",
    )

    with sqlite3.connect(db_path) as connection:
        frozen = json.loads(
            connection.execute("SELECT definition_json FROM team_subagent_revision WHERE id=?", (prepared.revision_id,)).fetchone()[0]
        )
        draft = json.loads(connection.execute("SELECT definition_json FROM team_subagent_working_copy WHERE id=?", (working.id,)).fetchone()[0])
    assert frozen["skill_grants"] == [{**grant, "revision": current}]
    # The exact definition that is frozen is the one validated last.
    assert validator.definitions[-1]["skill_grants"] == [{**grant, "revision": current}]
    # The Owner's draft is not rewritten.
    assert draft["skill_grants"] == [grant]


def test_publication_blocked_error_carries_blocked_items() -> None:
    item = {"kind": "skill", "name": "sales-report", "identity": "team:oskill_sales", "reason_codes": ["skill_pending_reapproval"]}
    validation = AgentPublicationValidation(
        compilation=dataclasses.replace(_compilation(), status="blocked", reason_codes=("skill_pending_reapproval",), blocked_items=(item,)),
        safety_status="unknown",
        deterministic_reason_codes=(),
        semantic_status="not_run",
        semantic_reason_codes=(),
    )
    store = AgentRevisionStore("unused.db", validator=_Validator(), clock=_Clock())

    with pytest.raises(AgentPublicationBlockedError) as raised:
        store._require_publishable(validation, audience=AgentAudience("private"))

    assert raised.value.code == "agent_dependency_blocked"
    assert raised.value.blocked_items == (item,)


async def _pending_teamwide_release(db_path) -> tuple[AgentRevisionStore, str]:
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(safety_status="review_required"),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Shared Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    assert pending.candidate_id is not None
    return store, pending.candidate_id


@pytest.mark.asyncio
async def test_working_copy_preserves_shadow_primary_but_publication_fails_closed(
    tmp_path,
) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    validator = DeterministicAgentPublicationValidator(
        catalog_store=object(),
        dependency_compiler=_DependencyCompiler(),  # type: ignore[arg-type]
        catalog_service=_CatalogService(),  # type: ignore[arg-type]
    )
    store = AgentRevisionStore(str(db_path), validator=validator, clock=_Clock())
    definition = {
        **_definition(),
        "primary_capability_ref": "cap_analysis@1",
    }

    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=definition,
        listing=_listing(),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )

    with sqlite3.connect(db_path) as connection:
        validation = json.loads(
            connection.execute(
                "SELECT validation_json FROM team_subagent_working_copy WHERE id=?",
                (working.id,),
            ).fetchone()[0],
        )
        revision_count = connection.execute(
            "SELECT COUNT(*) FROM team_subagent_revision",
        ).fetchone()[0]
    assert validation["status"] == "incomplete"
    assert validation["behavior"]["primary_capability_ref"] == "cap_analysis@1"

    with pytest.raises(
        AgentPublicationBlockedError,
        match="primary_capability_unproven",
    ) as error:
        await store.prepare_working_copy(
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=working.id,
            expected_working_copy_version=working.row_version,
            idempotency_key="primary-shadow-publish",
        )
    assert "primary_capability_unproven" in error.value.reason_codes
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM team_subagent_revision",
            ).fetchone()[0]
            == revision_count
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", [AgentAudience("custom", user_ids=("user_member",)), AgentAudience("teamwide")])
@pytest.mark.parametrize("definition_changed", [False, True])
async def test_personal_tool_binding_does_not_block_shared_release_and_only_follows_unchanged_definition(
    tmp_path, audience, definition_changed
) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    private_store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())
    first = await private_store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing(),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    live = await seed_legacy_publication(
        private_store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=first.id
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO personal_mcp_binding(agent_id,revision_id,owner_user_id,tool_ids_json,version) VALUES (?,?,?,?,?)",
            ("osa_release", live.revision_id, "user_owner", '["pmt_owner"]', 3),
        )
    shared_store = AgentRevisionStore(str(db_path), validator=_Validator(safety_status="review_required"), clock=_Clock())
    shared = await shared_store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Changed prompt." if definition_changed else "Be helpful."),
        listing=_listing(),
        audience=audience,
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        shared_store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=shared.id
    )
    assert pending.state == "pending"
    activated = await shared_store.approve_candidate(
        team_id="team_release",
        candidate_id=str(pending.candidate_id),
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=_candidate_hash_of(db_path, str(pending.candidate_id)),
    )
    assert activated.state == "activated"
    with sqlite3.connect(db_path) as connection:
        current = connection.execute(
            "SELECT tool_ids_json,version FROM personal_mcp_binding WHERE agent_id=? AND revision_id=?",
            ("osa_release", activated.revision_id),
        ).fetchone()
    assert current == (('["pmt_owner"]', 1) if not definition_changed else None)


@pytest.mark.asyncio
async def test_prepare_freezes_confirmed_listing_translation(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), eval_gate=AgentEvalReleaseGate(), clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing={**_listing("财务助手"), "source_locale": "zh-CN"},
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    localization_store = CatalogLocalizationStore(db_path)
    draft = await localization_store.save_agent_draft(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        locale="en-US",
        source_locale="zh-CN",
        fields={
            "display_name": "Finance Assistant",
            "summary": "A useful specialist.",
        },
        generation_source="llm",
        expected_source_hash=agent_listing_source_hash(
            {**_listing("财务助手"), "source_locale": "zh-CN"},
        ),
        expected_row_version=None,
    )
    await localization_store.confirm_agent_translation(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        locale="en-US",
        expected_row_version=draft.row_version,
    )

    async with aiosqlite.connect(db_path) as db:
        effective_listing = await materialize_confirmed_agent_localizations_conn(
            db,
            team_id="team_release",
            agent_id="osa_release",
            working_copy_id=working.id,
            listing={**_listing("财务助手"), "source_locale": "zh-CN"},
        )
    report = await AgentSafetyCheckService(
        str(db_path),
        validator=_Validator(),
        semantic_scanner=_SemanticScanner("pass", ()),
        clock=_Clock(),
    ).run_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
    )
    assert report.content_hash == _sha(
        _canonical({"definition": _definition(), "listing": effective_listing}),
    )

    result = await store.prepare_working_copy(
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=report.working_copy_row_version,
        idempotency_key="localized-private-publish",
    )

    with sqlite3.connect(db_path) as connection:
        listing = json.loads(
            connection.execute(
                "SELECT listing_json FROM team_subagent_revision WHERE id=?",
                (result.revision_id,),
            ).fetchone()[0],
        )
        localized_claim = connection.execute(
            "SELECT holder_kind,agent_id FROM agent_name_claim WHERE team_id='team_release' AND locale='en-us' AND normalized_key=?",
            (effective_agent_name_key("Finance Assistant"),),
        ).fetchone()
    assert listing["localized_listings"]["en-US"] == {
        "display_name": "Finance Assistant",
        "summary": "A useful specialist.",
    }
    assert listing["localization_metadata"]["en-US"]["generation_source"] == "llm"
    assert localized_claim == ("candidate", "osa_release")


@pytest.mark.asyncio
async def test_a_real_gate_never_turns_a_permission_boundary_into_a_gate_error(tmp_path) -> None:
    """Authorization comes before the gate's own writes.

    ``ensure_platform_contract_suite`` carries its own manager check, so calling it
    ahead of the ownership check answered "you do not own this Agent" with an
    unclassified Eval error -- which the release routes turn into a 500 that any team
    member can trigger.  The publish surface must keep answering `agent_owner_required`.
    """
    from bowhead_core.agent_eval_product import AgentEvalReleaseGate

    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    owner_store = AgentRevisionStore(str(db_path), validator=_Validator(), eval_gate=AgentEvalReleaseGate(), clock=_Clock())
    working = await owner_store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Guarded Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )

    gated = AgentRevisionStore(
        str(db_path),
        validator=_Validator(),
        eval_gate=AgentEvalReleaseGate(),
        clock=_Clock(),
    )
    with pytest.raises(AgentRevisionForbiddenError) as blocked:
        await gated.prepare_working_copy(
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_member",
            expected_working_copy_id=working.id,
            expected_working_copy_version=working.row_version,
            idempotency_key="not-the-owner",
        )
    assert blocked.value.code == "agent_owner_required"
    # ...and the refused caller left nothing behind.
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_eval_suite").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_real_gate_refuses_a_non_reviewer_before_installing_anything(tmp_path) -> None:
    """Same ordering rule on the approval surface."""
    from bowhead_core.agent_eval_product import AgentEvalReleaseGate

    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(),
        eval_gate=AgentEvalReleaseGate(),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Guarded Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    with pytest.raises(AgentRevisionForbiddenError) as blocked:
        await store.approve_candidate(
            team_id="team_release",
            candidate_id=str(pending.candidate_id),
            actor_user_id="user_member",
            expected_row_version=1,
            expected_candidate_hash=_candidate_hash_of(db_path, str(pending.candidate_id)),
        )
    assert blocked.value.code == "agent_release_review_forbidden"


@pytest.mark.parametrize(
    ("publish_m4", "approve_m4"),
    [("0", "1"), ("1", "0"), ("1", "1"), ("0", "0")],
)
@pytest.mark.asyncio
async def test_approval_reclaims_the_draft_across_a_localization_flag_flip(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    publish_m4: str,
    approve_m4: str,
) -> None:
    """A candidate frozen under one FEATURE_AGENT_INTELLIGENCE_M4 setting, approved under another.

    The flip itself puts every pending candidate in this position, so if reconcile
    compares a materialized listing against one frozen before the materializer ran,
    turning the flag on is what creates the phantom "unpublished draft" the Owner
    never wrote.  All four combinations have to reclaim the draft.
    """
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())

    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Straddle Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )

    await store.approve_candidate(
        team_id="team_release",
        candidate_id=str(pending.candidate_id),
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=_candidate_hash_of(db_path, str(pending.candidate_id)),
    )

    with sqlite3.connect(db_path) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM team_subagent_working_copy WHERE subagent_id='osa_release'",
        ).fetchone()[0]
    assert remaining == 0


@pytest.mark.asyncio
async def test_archive_and_restore_preserve_release_and_do_not_restore_access(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())

    archived = await store.archive_agent(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_aggregate_row_version=1,
    )
    assert archived.state == "archived"
    assert archived.aggregate_row_version == 2

    with sqlite3.connect(db_path) as connection:
        aggregate = connection.execute(
            """
            SELECT status,lifecycle_state,archived_by,live_release_id,row_version
            FROM team_subagent WHERE id='osa_release'
            """,
        ).fetchone()
        assignment = connection.execute(
            "SELECT personal_state,access_state FROM agent_assignment WHERE id='asa_release'",
        ).fetchone()
        name_count = connection.execute(
            "SELECT COUNT(*) FROM agent_name_claim WHERE agent_id='osa_release'",
        ).fetchone()[0]
    assert aggregate == ("disabled", "archived", "user_owner", "arl_initial", 2)
    assert assignment == ("active", "revoked")
    assert name_count == 0

    restored = await store.restore_agent(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_aggregate_row_version=2,
    )
    assert restored.state == "active"
    assert restored.aggregate_row_version == 3

    with sqlite3.connect(db_path) as connection:
        aggregate = connection.execute(
            """
            SELECT status,lifecycle_state,archived_at,archived_by,live_release_id,row_version
            FROM team_subagent WHERE id='osa_release'
            """,
        ).fetchone()
        assignment = connection.execute(
            "SELECT personal_state,access_state FROM agent_assignment WHERE id='asa_release'",
        ).fetchone()
        name = connection.execute(
            """
            SELECT holder_kind,agent_id FROM agent_name_claim
            WHERE team_id='team_release' AND locale='*' AND normalized_key=?
            """,
            (effective_agent_name_key("Old Agent"),),
        ).fetchone()
        actions = {
            str(row[0])
            for row in connection.execute(
                "SELECT action FROM admin_audit_log WHERE target_id='osa_release'",
            ).fetchall()
        }
    assert aggregate == ("enabled", "active", None, None, "arl_initial", 3)
    assert assignment == ("active", "revoked")
    assert name == ("agent", "osa_release")
    assert {"agent_archived", "agent_restored"}.issubset(actions)


@pytest.mark.asyncio
async def test_archive_withdraws_pending_candidate_atomically(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store, candidate_id = await _pending_teamwide_release(db_path)

    archived = await store.archive_agent(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_aggregate_row_version=1,
    )
    assert archived.pending_candidate_withdrawn is True

    with sqlite3.connect(db_path) as connection:
        candidate = connection.execute(
            """
            SELECT state,decision_reason_code FROM team_subagent_release_candidate
            WHERE id=?
            """,
            (candidate_id,),
        ).fetchone()
        claim_count = connection.execute(
            "SELECT COUNT(*) FROM agent_name_claim WHERE agent_id='osa_release'",
        ).fetchone()[0]
    assert candidate == ("withdrawn", "agent_archived")
    assert claim_count == 0


@pytest.mark.asyncio
async def test_archive_withdraws_prepared_candidate_so_restore_can_prepare_again(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(),
        eval_gate=AgentEvalReleaseGate(),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Candidate prompt."),
        listing=_listing("Candidate Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    prepared = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        idempotency_key="prepare-before-archive",
        release_note="Candidate validation",
    )
    assert prepared.state == "prepared"

    archived = await store.archive_agent(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_aggregate_row_version=1,
    )
    assert archived.pending_candidate_withdrawn is True

    with sqlite3.connect(db_path) as connection:
        candidate = connection.execute(
            "SELECT state,decision_reason_code FROM team_subagent_release_candidate WHERE id=?",
            (prepared.candidate_id,),
        ).fetchone()
        claim_count = connection.execute(
            "SELECT COUNT(*) FROM agent_name_claim WHERE agent_id='osa_release'",
        ).fetchone()[0]
        event = connection.execute(
            """
            SELECT id,payload_json FROM team_subagent_release_event
            WHERE candidate_id=? AND event_type='candidate.withdrawn'
            """,
            (prepared.candidate_id,),
        ).fetchone()
        assert event is not None
        outbox = connection.execute(
            """
            SELECT recipient_user_id,notification_type FROM team_subagent_notification_outbox
            WHERE release_event_id=?
            """,
            (event[0],),
        ).fetchall()
        withdrawn_audit = connection.execute(
            "SELECT COUNT(*) FROM admin_audit_log WHERE action=? AND target_id='osa_release'",
            (AuditAction.AGENT_RELEASE_CANDIDATE_WITHDRAWN,),
        ).fetchone()[0]
    assert candidate == ("withdrawn", "agent_archived")
    assert claim_count == 0
    assert json.loads(event[1]) == {"reason_code": "agent_archived"}
    assert outbox == [("user_owner", "agent.release.withdrawn")]
    assert withdrawn_audit == 1

    await store.restore_agent(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_aggregate_row_version=2,
    )
    refreshed = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Candidate prompt after restore."),
        listing=_listing("Candidate Agent"),
        audience=AgentAudience("private"),
        expected_row_version=working.row_version,
    )
    reprepared = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=refreshed.id,
        expected_working_copy_version=refreshed.row_version,
        expected_eval_policy_revision_id=prepared.policy_revision_id,
        workspace_id="ws_eval",
        idempotency_key="prepare-after-restore",
        release_note="Candidate validation",
    )
    assert reprepared.state == "prepared"
    assert reprepared.candidate_id != prepared.candidate_id


async def _prepared_candidate(tmp_path, *, idempotency_key: str):
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(),
        eval_gate=AgentEvalReleaseGate(),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Candidate prompt."),
        listing=_listing("Candidate Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    request = {
        "team_id": "team_release",
        "agent_id": "osa_release",
        "actor_user_id": "user_owner",
        "expected_working_copy_id": working.id,
        "expected_working_copy_version": working.row_version,
        "expected_eval_policy_revision_id": None,
        "workspace_id": "ws_eval",
        "idempotency_key": idempotency_key,
        "release_note": "Candidate validation",
    }
    prepared = await store.prepare_working_copy(**request)
    assert prepared.state == "prepared"
    return store, request, prepared


@pytest.mark.asyncio
async def test_prepare_replay_after_owner_withdrawal_reports_conflict(tmp_path) -> None:
    store, request, prepared = await _prepared_candidate(tmp_path, idempotency_key="prepare-then-withdraw")
    await store.withdraw_candidate(
        team_id="team_release",
        candidate_id=prepared.candidate_id,
        actor_user_id="user_owner",
        expected_row_version=prepared.candidate_row_version,
        expected_candidate_hash=prepared.candidate_hash,
    )

    with pytest.raises(AgentRevisionConflictError, match="agent_release_candidate_state_conflict"):
        await store.prepare_working_copy(**request)


@pytest.mark.asyncio
async def test_prepare_replay_after_archive_reports_inactive_agent_first(tmp_path) -> None:
    store, request, _prepared = await _prepared_candidate(tmp_path, idempotency_key="prepare-then-archive")
    await store.archive_agent(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_aggregate_row_version=1,
    )

    with pytest.raises(AgentPublicationBlockedError, match="agent_not_active"):
        await store.prepare_working_copy(**request)

    await store.restore_agent(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_aggregate_row_version=2,
    )
    # Restored, but the archived candidate stays withdrawn: the key is consumed.
    with pytest.raises(AgentRevisionConflictError, match="agent_release_candidate_state_conflict"):
        await store.prepare_working_copy(**request)


@pytest.mark.asyncio
async def test_post_cutover_create_has_private_release_and_rejects_legacy_writer(tmp_path) -> None:
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO teams(id,name,status,created_by_user_id,created_at)
            VALUES ('team_create','Team','active','user_creator',?)
            """,
            (_TIME,),
        )
        connection.execute(
            """
            INSERT INTO users(id,team_id,email,status,created_at)
            VALUES ('user_creator','team_create','creator@example.com','active',?)
            """,
            (_TIME,),
        )
        connection.execute(
            """
            INSERT INTO team_members(id,user_id,team_id,role,status,created_at)
            VALUES ('tm_creator','user_creator','team_create','team_admin','active',?)
            """,
            (_TIME,),
        )
        connection.execute(
            """
            UPDATE team_subagent_schema_meta
            SET release_semantics_activated_at=?,minimum_reader_version='0003',
                minimum_writer_version='0003'
            WHERE singleton=1
            """,
            (_TIME,),
        )
        connection.commit()

    revision_store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(),
        clock=_Clock(),
    )
    aggregate_store = TeamSubagentStore(db_path, clock=_Clock())
    created = TeamSubagent(
        id="osa_created",
        team_id="team_create",
        name="created-agent",
        display_name="Created Agent",
        system_prompt="Help carefully.",
        tool_refs=[],
        skills=[],
        visibility="creator_only",
        created_by="user_creator",
        created_at=_TIME,
        updated_at=_TIME,
    )
    await aggregate_store.create_team_subagent(
        created,
        actor_user_id="user_creator",
        audit_action=AuditAction.TEAM_SUBAGENT_CREATED,
        require_active_owner=True,
        revision_store=revision_store,
        revision_definition=_definition("Help carefully."),
        revision_listing=_listing("Created Agent"),
        revision_target_audience=AgentAudience("private"),
    )
    assert created.row_version == 2

    with sqlite3.connect(db_path) as connection:
        aggregate = connection.execute(
            """
            SELECT definition_mode,live_release_id,release_sequence,audience_mode,visibility
            FROM team_subagent WHERE id='osa_created'
            """,
        ).fetchone()
        assignment = connection.execute(
            """
            SELECT personal_state,access_state FROM agent_assignment
            WHERE team_id='team_create' AND user_id='user_creator' AND agent_id='osa_created'
            """,
        ).fetchone()
    assert aggregate is not None and aggregate[0] == "revisioned"
    assert str(aggregate[1]).startswith("arl_")
    assert aggregate[2:] == (1, "private", "creator_only")
    assert assignment == ("active", "granted")

    with pytest.raises(TeamSubagentRevisionWriterRequiredError):
        await aggregate_store.create_team_subagent(
            TeamSubagent(
                id="osa_legacy_bypass",
                team_id="team_create",
                name="legacy-bypass",
                created_by="user_creator",
                created_at=_TIME,
                updated_at=_TIME,
            ),
            actor_user_id="user_creator",
            audit_action=AuditAction.TEAM_SUBAGENT_CREATED,
            require_active_owner=True,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM team_subagent WHERE id='osa_legacy_bypass'",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_shared_publish_waits_for_admin_then_activates_frozen_revision(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    validator = _Validator(safety_status="review_required")
    store = AgentRevisionStore(str(db_path), validator=validator, clock=_Clock())
    audience = AgentAudience("custom", user_ids=("user_member",))
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Shared Agent"),
        audience=audience,
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    assert pending.state == "pending"
    with sqlite3.connect(db_path) as connection:
        candidate_hash = connection.execute(
            "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone()[0]
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_revision").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_release_event").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_notification_outbox").fetchone() == (1,)
        assert connection.execute("SELECT display_name FROM team_subagent WHERE id='osa_release'").fetchone() == ("Old Agent",)

    activated = await store.approve_candidate(
        team_id="team_release",
        candidate_id=str(pending.candidate_id),
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=str(candidate_hash),
    )
    assert activated.state == "activated"
    with sqlite3.connect(db_path) as connection:
        aggregate = connection.execute(
            "SELECT display_name,audience_mode,live_release_id FROM team_subagent WHERE id='osa_release'",
        ).fetchone()
        grants = connection.execute(
            """
            SELECT principal_type,principal_id FROM resource_grants
            WHERE team_id='team_release' AND resource_kind='team_subagent'
              AND resource_id='osa_release' AND effect='allow' AND revoked_at IS NULL
            """,
        ).fetchall()
        notifications = connection.execute(
            """
            SELECT notification_type,recipient_user_id
            FROM team_subagent_notification_outbox
            ORDER BY notification_type,recipient_user_id
            """,
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="release_event is immutable"):
            connection.execute(
                "UPDATE team_subagent_release_event SET event_type='candidate.stale'",
            )
        with pytest.raises(sqlite3.IntegrityError, match="outbox payload is immutable"):
            connection.execute(
                "UPDATE team_subagent_notification_outbox SET payload_json='{}'",
            )
        with pytest.raises(sqlite3.IntegrityError, match="candidate payload is immutable"):
            connection.execute(
                "UPDATE team_subagent_release_candidate SET release_note='tampered'",
            )
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_other','Other','active',?)",
            (_TIME,),
        )
        connection.execute(
            """
            INSERT INTO users(id,team_id,email,status,created_at)
            VALUES ('user_other','team_other','other@example.com','active',?)
            """,
            (_TIME,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="actor belongs to another team"):
            connection.execute(
                """
                INSERT INTO team_subagent_release_event(
                  id,team_id,subagent_id,release_id,event_type,actor_user_id,
                  payload_json,created_at
                ) VALUES (
                  'are_cross_actor','team_release','osa_release',?,
                  'release.activated','user_other','{}',?
                )
                """,
                (activated.release_id, _TIME),
            )
        release_event_id = connection.execute(
            "SELECT id FROM team_subagent_release_event WHERE release_id=?",
            (activated.release_id,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="recipient belongs to another team"):
            connection.execute(
                """
                INSERT INTO team_subagent_notification_outbox(
                  id,release_event_id,recipient_user_id,notification_type,dedupe_key,
                  payload_json,state,next_attempt_at,created_at
                ) VALUES (
                  'ano_cross_recipient',?,'user_other','agent.release.updated',
                  'cross-recipient','{}','pending',?,?
                )
                """,
                (release_event_id, _TIME, _TIME),
            )
        connection.rollback()
    assert aggregate == ("Shared Agent", "custom", activated.release_id)
    assert grants == [("user", "user_member")]
    assert notifications == [
        ("agent.release.approval_required", "user_admin"),
        ("agent.release.updated", "user_member"),
        ("agent.release.updated", "user_owner"),
    ]


@pytest.mark.asyncio
async def test_prepare_installs_the_platform_contract_suite(tmp_path) -> None:
    """The first preparation installs its required platform suite."""
    from bowhead_core.agent_eval_product import AgentEvalReleaseGate

    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(safety_status="pass"),
        eval_gate=AgentEvalReleaseGate(),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Suite Installing Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_eval_suite").fetchone()[0] == 0

    pending = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        idempotency_key="prepare-platform-suite",
    )

    # The suite now exists, installed by the preparation path itself.
    with sqlite3.connect(db_path) as connection:
        suites = connection.execute(
            "SELECT suite_kind,platform_managed FROM agent_eval_suite",
        ).fetchall()
    assert suites == [("platform_contract", 1)]

    assert pending.state == "prepared"
    assert pending.plan_id and pending.policy_revision_id


@pytest.mark.asyncio
async def test_eval_gate_forces_shared_candidate_and_persists_server_evidence(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    gate = _EvalGate()
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(safety_status="pass"),
        eval_gate=gate,
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Eval-gated Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    assert pending.state == "pending"
    assert pending.candidate_id is not None
    with sqlite3.connect(db_path) as connection:
        candidate_hash, candidate_evidence = connection.execute(
            "SELECT candidate_hash,eval_evidence_json FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone()
    assert candidate_evidence is None

    activated = await store.approve_candidate(
        team_id="team_release",
        candidate_id=pending.candidate_id,
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=str(candidate_hash),
    )
    assert gate.candidate_ids == [pending.candidate_id]
    with sqlite3.connect(db_path) as connection:
        release_evidence = connection.execute(
            "SELECT eval_evidence_json FROM team_subagent_release WHERE id=?",
            (activated.release_id,),
        ).fetchone()[0]
    assert json.loads(str(release_evidence)) == {
        "kind": "agent_eval_release_evidence",
        "runs": [{"run_id": "aer_release_contract"}],
        "schema_version": 1,
    }


@pytest.mark.asyncio
async def test_approval_rebases_newer_working_copy_without_changing_candidate(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(safety_status="review_required"),
        clock=_Clock(),
    )
    audience = AgentAudience("teamwide")
    submitted = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Submitted Agent"),
        audience=audience,
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=submitted.id
    )
    next_draft = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(prompt="Instructions for the next release"),
        listing=_listing("Next Agent"),
        audience=audience,
        expected_row_version=submitted.row_version,
    )
    with sqlite3.connect(db_path) as connection:
        candidate_hash = connection.execute(
            "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone()[0]

    activated = await store.approve_candidate(
        team_id="team_release",
        candidate_id=str(pending.candidate_id),
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=str(candidate_hash),
    )

    reads = AgentReleaseReadStore(str(db_path))
    detail = await reads.working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
    )
    assert detail["live"]["listing"]["display_name"] == "Submitted Agent"
    assert detail["working_copy"]["listing"]["display_name"] == "Next Agent"
    assert detail["working_copy"]["base_release_id"] == activated.release_id
    assert detail["working_copy"]["row_version"] == next_draft.row_version + 1
    assert detail["working_copy"]["validation"] == {"status": "not_validated"}

    saved_again = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(prompt="Instructions for the next release, revised"),
        listing=_listing("Next Agent"),
        audience=audience,
        expected_row_version=detail["working_copy"]["row_version"],
    )
    assert saved_again.base_release_id == activated.release_id


@pytest.mark.asyncio
async def test_approval_dependency_drift_marks_candidate_stale_and_releases_name(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    validator = _Validator(safety_status="review_required", token="before")
    store = AgentRevisionStore(str(db_path), validator=validator, clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Reserved Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    with sqlite3.connect(db_path) as connection:
        candidate_hash = connection.execute(
            "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone()[0]
    validator.token = "after"
    with pytest.raises(AgentReleaseStaleError) as caught:
        await store.approve_candidate(
            team_id="team_release",
            candidate_id=str(pending.candidate_id),
            actor_user_id="user_admin",
            expected_row_version=1,
            expected_candidate_hash=str(candidate_hash),
        )
    assert "dependency_fingerprint_changed" in caught.value.reason_codes
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,row_version FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone() == ("stale", 2)
        assert (
            connection.execute(
                """
            SELECT 1 FROM agent_name_claim
            WHERE team_id='team_release' AND locale='*' AND normalized_key=?
            """,
                (effective_agent_name_key("Reserved Agent"),),
            ).fetchone()
            is None
        )
        assert connection.execute("SELECT live_release_id FROM team_subagent WHERE id='osa_release'").fetchone() == ("arl_initial",)


@pytest.mark.asyncio
async def test_approval_fails_closed_when_frozen_behavior_sidecar_is_missing(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(safety_status="review_required"),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Behavior Evidence Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    with sqlite3.connect(db_path) as connection:
        candidate_hash = connection.execute(
            "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone()[0]
        connection.execute(
            "DROP TRIGGER trg_team_subagent_revision_behavior_immutable_delete",
        )
        connection.execute(
            "DELETE FROM team_subagent_revision_behavior WHERE revision_id=?",
            (pending.revision_id,),
        )
        connection.commit()

    with pytest.raises(AgentReleaseStaleError) as caught:
        await store.approve_candidate(
            team_id="team_release",
            candidate_id=str(pending.candidate_id),
            actor_user_id="user_admin",
            expected_row_version=1,
            expected_candidate_hash=str(candidate_hash),
        )

    assert "behavior_descriptor_missing" in caught.value.reason_codes
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone() == ("stale",)
        assert connection.execute(
            "SELECT live_release_id FROM team_subagent WHERE id='osa_release'",
        ).fetchone() == ("arl_initial",)


@pytest.mark.asyncio
async def test_approval_recomputes_candidate_hash_if_storage_was_corrupted(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(safety_status="review_required"),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Integrity Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    tampered_hash = _sha("tampered-candidate")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER trg_team_subagent_release_candidate_payload_immutable")
        connection.execute(
            "UPDATE team_subagent_release_candidate SET candidate_hash=? WHERE id=?",
            (tampered_hash, pending.candidate_id),
        )
        connection.commit()
    with pytest.raises(AgentReleaseStaleError) as caught:
        await store.approve_candidate(
            team_id="team_release",
            candidate_id=str(pending.candidate_id),
            actor_user_id="user_admin",
            expected_row_version=1,
            expected_candidate_hash=tampered_hash,
        )
    assert "candidate_hash_changed" in caught.value.reason_codes
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone() == ("stale",)
        assert connection.execute(
            "SELECT live_release_id FROM team_subagent WHERE id='osa_release'",
        ).fetchone() == ("arl_initial",)


@pytest.mark.asyncio
async def test_approval_marks_candidate_stale_after_owner_transfer(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(safety_status="review_required"),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Transferred Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    with sqlite3.connect(db_path) as connection:
        candidate_hash = connection.execute(
            "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE team_subagent SET owner_user_id='user_member',row_version=row_version+1 WHERE id='osa_release'",
        )
        connection.commit()
    with pytest.raises(AgentReleaseStaleError) as caught:
        await store.approve_candidate(
            team_id="team_release",
            candidate_id=str(pending.candidate_id),
            actor_user_id="user_admin",
            expected_row_version=1,
            expected_candidate_hash=str(candidate_hash),
        )
    assert caught.value.reason_codes == ("agent_owner_changed",)


@pytest.mark.asyncio
async def test_approval_keeps_candidate_pending_when_owner_is_inactive(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(safety_status="review_required"),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Inactive Owner Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    with sqlite3.connect(db_path) as connection:
        candidate_hash = connection.execute(
            "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone()[0]
        connection.execute("UPDATE users SET status='disabled' WHERE id='user_owner'")
        connection.commit()
    with pytest.raises(AgentPublicationBlockedError, match="agent_owner_inactive"):
        await store.approve_candidate(
            team_id="team_release",
            candidate_id=str(pending.candidate_id),
            actor_user_id="user_admin",
            expected_row_version=1,
            expected_candidate_hash=str(candidate_hash),
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone() == ("pending",)


@pytest.mark.asyncio
async def test_outbox_insert_failure_rolls_back_entire_activation(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Atomic Agent"),
        audience=AgentAudience("custom", user_ids=("user_member",)),
        expected_row_version=None,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_release_outbox BEFORE INSERT ON team_subagent_notification_outbox
            BEGIN SELECT RAISE(ABORT, 'forced outbox failure'); END
            """,
        )
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
        await seed_legacy_publication(store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT display_name,live_release_id,release_sequence,row_version FROM team_subagent WHERE id='osa_release'",
        ).fetchone() == ("Old Agent", "arl_initial", 1, 1)
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_revision").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_release_candidate").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_release_event").fetchone() == (0,)


@pytest.mark.asyncio
async def test_behavior_sidecar_failure_rolls_back_new_team_revision(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), eval_gate=AgentEvalReleaseGate(), clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Atomic Behavior"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_team_behavior BEFORE INSERT ON team_subagent_revision_behavior
            BEGIN SELECT RAISE(ABORT, 'forced behavior failure'); END
            """,
        )
        connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced behavior failure"):
        await store.prepare_working_copy(
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=working.id,
            expected_working_copy_version=working.row_version,
            idempotency_key="atomic-behavior-publish",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_revision").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_revision_behavior").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_release_candidate").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM admin_audit_log").fetchone() == (0,)


@pytest.mark.asyncio
async def test_audience_contraction_reuses_live_revision_without_safety_gate(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Shared Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    published = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    assert published.state == "pending"
    with sqlite3.connect(db_path) as connection:
        candidate_hash = connection.execute(
            "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
            (published.candidate_id,),
        ).fetchone()[0]
    published = await store.approve_candidate(
        team_id="team_release",
        candidate_id=str(published.candidate_id),
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=str(candidate_hash),
    )
    contracted = await store.contract_audience(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_aggregate_row_version=published.aggregate_row_version,
        audience=AgentAudience("private"),
        idempotency_key="contract-1",
        reason_code="owner_removed_audience",
    )
    assert contracted.state == "activated"
    assert contracted.revision_id == published.revision_id
    assert contracted.release_id != published.release_id
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT audience_mode,release_sequence,live_revision_id FROM team_subagent WHERE id='osa_release'",
        ).fetchone() == ("private", 3, published.revision_id)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM resource_grants
            WHERE resource_kind='team_subagent' AND resource_id='osa_release'
              AND effect='allow' AND revoked_at IS NULL
            """,
        ).fetchone() == (0,)


async def _teamwide_live_store(db_path) -> tuple[AgentRevisionStore, AgentRevisionStore, int]:
    """Make a teamwide release live (so it can be contracted later).

    Returns a store for historical recovery, one with the Owner evaluation gate and the
    aggregate row version.
    """
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())
    gated = AgentRevisionStore(str(db_path), validator=_Validator(), eval_gate=AgentEvalReleaseGate(), clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Shared Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    published = await store.approve_candidate(
        team_id="team_release",
        candidate_id=str(pending.candidate_id),
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=_candidate_hash_of(db_path, str(pending.candidate_id)),
    )
    return store, gated, int(published.aggregate_row_version)


async def _contract_and_redraft(store: AgentRevisionStore, db_path, *, aggregate_row_version: int, working_id: str, working_version: int):
    """Move live without touching the candidate (audience contraction), then start a draft on the new live."""
    contracted = await store.contract_audience(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_aggregate_row_version=aggregate_row_version,
        audience=AgentAudience("private"),
        idempotency_key="contract-under-candidate",
        reason_code="owner_removed_audience",
    )
    assert contracted.state == "activated"
    assert await store.discard_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working_id,
        expected_row_version=working_version,
    )
    return await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Prompt on the new live."),
        listing=_listing("Shared Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )


def _candidate_state(db_path, candidate_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT state,decision_reason_code FROM team_subagent_release_candidate WHERE id=?",
            (candidate_id,),
        ).fetchone()


@pytest.mark.asyncio
async def test_prepare_retires_a_prepared_candidate_whose_base_release_is_no_longer_live(tmp_path) -> None:
    db_path = _db(tmp_path)
    _plain, store, aggregate_row_version = await _teamwide_live_store(db_path)
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Candidate prompt."),
        listing=_listing("Shared Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    first = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        idempotency_key="prepare-on-old-live",
        release_note="First",
    )
    assert first.state == "prepared"
    redraft = await _contract_and_redraft(
        store, db_path, aggregate_row_version=aggregate_row_version, working_id=working.id, working_version=working.row_version
    )
    assert _candidate_state(db_path, str(first.candidate_id)) == ("prepared", None)

    second = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=redraft.id,
        expected_working_copy_version=redraft.row_version,
        expected_eval_policy_revision_id=first.policy_revision_id,
        workspace_id="ws_eval",
        idempotency_key="prepare-on-new-live",
        release_note="Second",
    )
    assert second.state == "prepared"
    assert second.candidate_id != first.candidate_id
    assert _candidate_state(db_path, str(first.candidate_id)) == ("stale", "base_release_changed")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT payload_json FROM team_subagent_release_event WHERE candidate_id=? AND event_type='candidate.stale'",
            (first.candidate_id,),
        ).fetchone() == (json.dumps({"reason_codes": ["base_release_changed"]}, separators=(",", ":")),)


@pytest.mark.asyncio
async def test_policy_conflict_on_prepare_names_the_current_policy_revision(tmp_path) -> None:
    # 10-14 dogfood: CI omitted the policy revision once a policy existed and got a bare 409 it
    # could only resolve by copying the id out of the Owner's browser session.
    db_path = _db(tmp_path)
    _plain, store, aggregate_row_version = await _teamwide_live_store(db_path)
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Candidate prompt."),
        listing=_listing("Shared Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    first = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        idempotency_key="prepare-creates-policy",
    )
    redraft = await _contract_and_redraft(
        store, db_path, aggregate_row_version=aggregate_row_version, working_id=working.id, working_version=working.row_version
    )
    with pytest.raises(AgentRevisionConflictError, match="agent_eval_policy_version_conflict") as conflict:
        await store.prepare_working_copy(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=redraft.id,
            expected_working_copy_version=redraft.row_version,
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            idempotency_key="prepare-without-policy",
        )
    assert conflict.value.details == {"current_policy_revision_id": first.policy_revision_id}


@pytest.mark.asyncio
async def test_prepare_retires_a_pending_candidate_whose_base_release_is_no_longer_live(tmp_path) -> None:
    db_path = _db(tmp_path)
    plain, store, aggregate_row_version = await _teamwide_live_store(db_path)
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Pending prompt."),
        listing=_listing("Shared Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        plain, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    assert pending.state == "pending"
    redraft = await _contract_and_redraft(
        store, db_path, aggregate_row_version=aggregate_row_version, working_id=working.id, working_version=working.row_version
    )

    prepared = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=redraft.id,
        expected_working_copy_version=redraft.row_version,
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        idempotency_key="prepare-after-pending",
        release_note="Replacement",
    )
    assert prepared.state == "prepared"
    assert _candidate_state(db_path, str(pending.candidate_id)) == ("stale", "base_release_changed")


@pytest.mark.asyncio
async def test_candidate_decisions_require_owner_or_qualified_reviewer(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    validator = _Validator(safety_status="review_required")
    store = AgentRevisionStore(str(db_path), validator=validator, clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Protected Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    with sqlite3.connect(db_path) as connection:
        candidate_hash = connection.execute(
            "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone()[0]
    with pytest.raises(AgentRevisionForbiddenError):
        await store.approve_candidate(
            team_id="team_release",
            candidate_id=str(pending.candidate_id),
            actor_user_id="user_member",
            expected_row_version=1,
            expected_candidate_hash=str(candidate_hash),
        )
    with pytest.raises(AgentRevisionForbiddenError):
        await store.withdraw_candidate(
            team_id="team_release",
            candidate_id=str(pending.candidate_id),
            actor_user_id="user_member",
            expected_row_version=1,
            expected_candidate_hash=str(candidate_hash),
        )


@pytest.mark.asyncio
async def test_reject_records_self_review_truthfully_and_releases_name(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    validator = _Validator(safety_status="review_required")
    store = AgentRevisionStore(str(db_path), validator=validator, clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Rejected Agent"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    pending = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )
    with sqlite3.connect(db_path) as connection:
        candidate_hash = connection.execute(
            "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
            (pending.candidate_id,),
        ).fetchone()[0]
    rejected = await store.reject_candidate(
        team_id="team_release",
        candidate_id=str(pending.candidate_id),
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=str(candidate_hash),
        reason_code="policy_not_met",
    )
    assert rejected.state == "rejected"
    with sqlite3.connect(db_path) as connection:
        decision = connection.execute(
            """
            SELECT state,self_review,decision_reason_code
            FROM team_subagent_release_candidate WHERE id=?
            """,
            (pending.candidate_id,),
        ).fetchone()
        claim = connection.execute(
            "SELECT 1 FROM agent_name_claim WHERE normalized_key=?",
            (effective_agent_name_key("Rejected Agent"),),
        ).fetchone()
    assert decision == ("rejected", 0, "policy_not_met")
    assert claim is None


@pytest.mark.asyncio
async def test_blocked_safety_never_freezes_revision_or_name(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(safety_status="blocked"),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Blocked Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    with pytest.raises(AgentPublicationBlockedError):
        await store.prepare_working_copy(
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=working.id,
            expected_working_copy_version=working.row_version,
            idempotency_key="blocked-publish",
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_revision").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM team_subagent_release_candidate").fetchone() == (0,)
        assert (
            connection.execute(
                "SELECT 1 FROM agent_name_claim WHERE normalized_key=?",
                (effective_agent_name_key("Blocked Agent"),),
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_invalid_live_projection_never_freezes_candidate(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), eval_gate=AgentEvalReleaseGate(), clock=_Clock())
    definition = _definition()
    definition["system_prompt"] = 42
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=definition,
        listing=_listing("Invalid Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    with pytest.raises(AgentPublicationBlockedError, match="projection_invalid"):
        await store.prepare_working_copy(
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=working.id,
            expected_working_copy_version=working.row_version,
            idempotency_key="invalid-projection",
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM team_subagent_release_candidate",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_release_note_secret_is_blocked_before_candidate_freeze(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), eval_gate=AgentEvalReleaseGate(), clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Release Note Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    with pytest.raises(AgentPublicationBlockedError, match="release_note_blocked") as caught:
        await store.prepare_working_copy(
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=working.id,
            expected_working_copy_version=working.row_version,
            idempotency_key="unsafe-release-note",
            release_note="API_TOKEN=sk-abcdefghijklmnopqrstuvwxyz1234567890",
        )
    assert caught.value.reason_codes == ("credential_or_secret",)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM team_subagent_release_candidate",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_localized_display_name_locale_is_canonical_and_unique(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())
    listing = {
        **_listing("Localized Agent"),
        "localized_display_names": {"en-US": "One", "en-us": "Two"},
    }
    with pytest.raises(ValueError, match="locale is duplicated"):
        await store.save_working_copy(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            definition=_definition(),
            listing=listing,
            audience=AgentAudience("private"),
            expected_row_version=None,
        )


def test_audience_shape_is_canonical() -> None:
    assert AgentAudience(
        "custom",
        user_ids=("u1", "u2"),
        group_ids=("g1",),
    ).to_dict() == {
        "effective_mode": "custom",
        "group_ids": ["g1"],
        "schema_version": 1,
        "user_ids": ["u1", "u2"],
    }
    with pytest.raises(ValueError):
        AgentAudience("custom", user_ids=("u2", "u1"))
    with pytest.raises(ValueError):
        AgentAudience("private", user_ids=("u1",))
    with pytest.raises(ValueError):
        AgentAudience("custom")


@pytest.mark.asyncio
async def test_pending_candidate_rejects_different_payload(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(
        str(db_path),
        validator=_Validator(safety_status="review_required"),
        clock=_Clock(),
    )
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("First Candidate"),
        audience=AgentAudience("teamwide"),
        expected_row_version=None,
    )
    await seed_legacy_publication(store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id)
    changed = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Changed while pending."),
        listing=_listing("Second Candidate"),
        audience=AgentAudience("teamwide"),
        expected_row_version=working.row_version,
    )
    with pytest.raises(AgentRevisionConflictError, match="already_active"):
        await store.prepare_working_copy(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=changed.id,
            expected_working_copy_version=changed.row_version,
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            idempotency_key="different-candidate",
        )


@pytest.mark.asyncio
async def test_release_outbox_projects_once_and_resolves_approval(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store, candidate_id = await _pending_teamwide_release(db_path)
    notifications = NotificationStore(str(db_path))
    await notifications.initialize()
    outbox = AgentReleaseNotificationOutbox(
        str(db_path),
        notification_store=notifications,
        clock=_Clock(),
    )

    first = await outbox.drain_once(worker_id="worker-one")
    assert (first.claimed, first.delivered, first.retried, first.failed) == (1, 1, 0, 0)
    with sqlite3.connect(db_path) as connection:
        notification = connection.execute(
            """
            SELECT id,type,priority,subject_type,subject_id,action_url,state
            FROM notification_items
            """,
        ).fetchone()
        assert notification is not None
        notification_id = notification[0]
        assert notification[1:] == (
            "agent.release.approval_required",
            "p2",
            "agent_release_candidate",
            candidate_id,
            f"/agents/manage/?release_candidate={candidate_id}",
            "open",
        )
        assert connection.execute(
            "SELECT user_id FROM notification_recipients WHERE notification_id=?",
            (notification_id,),
        ).fetchall() == [("user_admin",)]
        # Display name is snapshotted at projection for the localized title.
        metadata = json.loads(connection.execute("SELECT metadata_json FROM notification_items").fetchone()[0])
        assert metadata["agentName"] == "Old Agent"
        connection.execute(
            """
            UPDATE team_subagent_notification_outbox
            SET state='pending',delivered_at=NULL,next_attempt_at=?
            """,
            (_TIME,),
        )
        connection.commit()

    duplicate = await outbox.drain_once(worker_id="worker-two")
    assert duplicate.delivered == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_items").fetchone() == (1,)
        candidate_hash = connection.execute(
            "SELECT candidate_hash FROM team_subagent_release_candidate WHERE id=?",
            (candidate_id,),
        ).fetchone()[0]
    await store.approve_candidate(
        team_id="team_release",
        candidate_id=candidate_id,
        actor_user_id="user_admin",
        expected_row_version=1,
        expected_candidate_hash=str(candidate_hash),
    )
    terminal = await outbox.drain_once(worker_id="worker-three")
    assert terminal.delivered >= 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT state,resolved_reason FROM notification_items
            WHERE subject_type='agent_release_candidate' AND subject_id=?
            """,
            (candidate_id,),
        ).fetchone() == ("resolved", "release.activated")


@pytest.mark.asyncio
async def test_release_outbox_failure_requires_admin_replay(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    await _pending_teamwide_release(db_path)
    notifications = NotificationStore(str(db_path))
    await notifications.initialize()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_agent_release_notification
            BEFORE INSERT ON notification_items
            WHEN NEW.type='agent.release.approval_required'
            BEGIN
              SELECT RAISE(ABORT, 'synthetic notification failure');
            END
            """,
        )
        connection.commit()
    outbox = AgentReleaseNotificationOutbox(
        str(db_path),
        notification_store=notifications,
        clock=_Clock(),
        max_attempts=1,
    )
    result = await outbox.drain_once(worker_id="worker-failing")
    assert (result.failed, result.retried) == (1, 0)
    with sqlite3.connect(db_path) as connection:
        event_id, error_code = connection.execute(
            """
            SELECT release_event_id,last_error_code
            FROM team_subagent_notification_outbox WHERE state='failed'
            """,
        ).fetchone()
        assert error_code == "agent_release_notification_projection_failed"

    with pytest.raises(AgentReleaseOutboxForbiddenError):
        await outbox.replay_failed_event(
            team_id="team_release",
            event_id=str(event_id),
            actor_user_id="user_member",
        )
    assert (
        await outbox.replay_failed_event(
            team_id="team_release",
            event_id=str(event_id),
            actor_user_id="user_admin",
        )
        == 1
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT state,attempt_count,last_error_code
            FROM team_subagent_notification_outbox
            """,
        ).fetchone() == ("pending", 0, None)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM admin_audit_log
            WHERE action='agent_release_notification_replayed'
              AND actor_user_id='user_admin' AND target_id='osa_release'
            """,
        ).fetchone() == (1,)
        connection.execute("DROP TRIGGER fail_agent_release_notification")
        connection.commit()
    delivered = await outbox.drain_once(worker_id="worker-replay")
    assert delivered.delivered == 1


@pytest.mark.asyncio
async def test_release_inbox_and_reviewer_detail_are_candidate_scoped(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _, candidate_id = await _pending_teamwide_release(db_path)
    reads = AgentReleaseReadStore(str(db_path))

    assert (
        await reads.inbox_count(
            team_id="team_release",
            actor_user_id="user_admin",
        )
        == 1
    )
    inbox = await reads.inbox(
        team_id="team_release",
        actor_user_id="user_admin",
    )
    assert [item["candidate_id"] for item in inbox] == [candidate_id]
    assert inbox[0]["audience"] == {
        "mode": "teamwide",
        "user_count": 0,
        "group_count": 0,
    }

    detail = await reads.candidate_detail(
        team_id="team_release",
        candidate_id=candidate_id,
        actor_user_id="user_admin",
    )
    serialized = json.dumps(detail)
    assert detail["actions"] == {
        "approve": True,
        "reject": True,
        "withdraw": False,
    }
    assert detail["diff"]["listing_fields"] == ["display_name"]
    assert detail["diff"]["primary_capability_changed"] is False
    assert detail["diff"]["capability_protocol_block_changed"] is False
    assert detail["diff"]["behavior_changed"] is True
    assert detail["candidate"]["behavior"] == {
        "model": None,
        "max_iterations": 10,
        "has_output_contract": False,
        "tool_count": 0,
        "skill_count": 0,
        "runtime_extensions": None,
        "capability_protocol_block": False,
        "requested_primary_capability_ref": None,
        "primary_capability_ref": None,
        "primary_capability_name": None,
        "primary_capability_name_en": None,
        "verification_state": "shadow",
        "eligible_role_ids": [],
        "output_contract_refs": [],
    }
    assert "Old prompt." not in serialized
    assert "Be helpful." not in serialized
    assert "system_prompt" not in serialized
    assert "tool_refs" not in serialized
    assert "user_member" not in serialized
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM admin_audit_log
            WHERE action='agent_release_candidate_viewed'
              AND actor_user_id='user_admin' AND target_id=?
            """,
            (candidate_id,),
        ).fetchone() == (1,)

    with pytest.raises(AgentRevisionForbiddenError):
        await reads.inbox(
            team_id="team_release",
            actor_user_id="user_member",
        )
    with pytest.raises(AgentRevisionForbiddenError):
        await reads.candidate_detail(
            team_id="team_release",
            candidate_id=candidate_id,
            actor_user_id="user_member",
        )


@pytest.mark.asyncio
async def test_rollback_creates_new_draft_release_and_keeps_current_audience(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())
    changed = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("New prompt."),
        listing=_listing("New Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    activated = await seed_legacy_publication(
        store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=changed.id
    )

    rollback = await store.create_rollback_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        source_release_id="arl_initial",
        expected_aggregate_row_version=activated.aggregate_row_version,
        expected_working_copy_version=None,
    )
    replay = await store.create_rollback_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        source_release_id="arl_initial",
        expected_aggregate_row_version=activated.aggregate_row_version,
        expected_working_copy_version=None,
    )
    assert replay == rollback
    assert rollback.definition["system_prompt"] == "Old prompt."
    assert rollback.audience == AgentAudience("private")
    reads = AgentReleaseReadStore(str(db_path))
    working = await reads.working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
    )
    assert working["working_copy"]["change_kind"] == "rollback"
    assert working["working_copy"]["rollback_source_release_id"] == "arl_initial"

    restored = await seed_legacy_publication(
        store,
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        working_copy_id=rollback.id,
        release_note="Restore the original behavior",
    )
    assert restored.state == "activated"
    history = await reads.releases(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
    )
    assert [item["release_sequence"] for item in history] == [3, 2, 1]
    assert history[0]["change_kind"] == "rollback"


@pytest.mark.asyncio
async def test_semantic_safety_report_is_persisted_and_consumed_by_prepare(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    semantic_resolver = PersistedAgentSemanticEvidenceResolver()
    validator = DeterministicAgentPublicationValidator(
        catalog_store=object(),
        dependency_compiler=_DependencyCompiler(),  # type: ignore[arg-type]
        catalog_service=_CatalogService(),  # type: ignore[arg-type]
        semantic_evidence_resolver=semantic_resolver,
    )
    store = AgentRevisionStore(str(db_path), validator=validator, eval_gate=AgentEvalReleaseGate(), clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("Semantic Review Agent"),
        audience=AgentAudience("custom", user_ids=("user_member",)),
        expected_row_version=None,
    )
    scanner = _SemanticScanner("needs_review", ("data_handling_ambiguity",))
    service = AgentSafetyCheckService(
        str(db_path),
        validator=validator,
        semantic_scanner=scanner,
        clock=_Clock(),
    )
    report = await service.run_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
    )
    assert scanner.calls == 1
    assert report.status == "review_required"
    assert report.semantic == {
        "reason_codes": ["data_handling_ambiguity"],
        "status": "needs_review",
    }
    assert report.deterministic == {"reason_codes": [], "status": "pass"}
    assert report.working_copy_row_version == 2

    result = await store.prepare_working_copy(
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=report.working_copy_row_version,
        idempotency_key="semantic-evidence-publish",
    )
    assert result.state == "prepared"
    with sqlite3.connect(db_path) as connection:
        frozen = connection.execute(
            """
            SELECT status,semantic_scanner_version,semantic_json
            FROM agent_safety_report
            WHERE subject_kind='revision' AND subject_ref=?
            """,
            (result.revision_id,),
        ).fetchone()
    assert frozen == (
        "review_required",
        "test-semantic-v1",
        _canonical(
            {
                "reason_codes": ["data_handling_ambiguity"],
                "status": "needs_review",
            },
        ),
    )


@pytest.mark.asyncio
async def test_server_side_semantic_evidence_reaches_prepare_without_moving_the_pinned_version(tmp_path) -> None:
    # CI and Coach pin the accepted working-copy version, so the evidence step must not bump it,
    # and the frozen revision must carry the scan instead of "scanner unavailable".
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    validator = DeterministicAgentPublicationValidator(
        catalog_store=object(),
        dependency_compiler=_DependencyCompiler(),  # type: ignore[arg-type]
        catalog_service=_CatalogService(),  # type: ignore[arg-type]
        semantic_evidence_resolver=PersistedAgentSemanticEvidenceResolver(),
    )
    store = AgentRevisionStore(str(db_path), validator=validator, eval_gate=AgentEvalReleaseGate(), clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(),
        listing=_listing("CI Prepared Agent"),
        audience=AgentAudience("custom", user_ids=("user_member",)),
        expected_row_version=None,
    )
    scanner = _SemanticScanner("pass", ())
    service = AgentSafetyCheckService(str(db_path), validator=validator, semantic_scanner=scanner, clock=_Clock())
    for _ in range(2):
        await service.record_semantic_evidence(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=working.id,
            expected_working_copy_version=working.row_version,
        )
    assert scanner.calls == 1

    result = await store.prepare_working_copy(
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
        idempotency_key="ci-semantic-evidence",
    )
    assert result.state == "prepared"
    with sqlite3.connect(db_path) as connection:
        frozen = connection.execute(
            "SELECT status,semantic_json FROM agent_safety_report WHERE subject_kind='revision' AND subject_ref=?",
            (result.revision_id,),
        ).fetchone()
    assert frozen == ("pass", _canonical({"reason_codes": [], "status": "pass"}))


@pytest.mark.asyncio
async def test_deterministic_block_skips_semantic_and_cannot_be_overridden(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    validator = DeterministicAgentPublicationValidator(
        catalog_store=object(),
        dependency_compiler=_DependencyCompiler(),  # type: ignore[arg-type]
        catalog_service=_CatalogService(),  # type: ignore[arg-type]
        semantic_evidence_resolver=PersistedAgentSemanticEvidenceResolver(),
    )
    store = AgentRevisionStore(str(db_path), validator=validator, clock=_Clock())
    secret = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition(f"API_TOKEN={secret}"),
        listing=_listing("Blocked Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    scanner = _SemanticScanner("pass", ())
    report = await AgentSafetyCheckService(
        str(db_path),
        validator=validator,
        semantic_scanner=scanner,
        clock=_Clock(),
    ).run_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
    )
    assert scanner.calls == 0
    assert report.status == "blocked"
    assert report.semantic_scanner_version == "none"
    assert secret not in repr(report)
    with pytest.raises(AgentPublicationBlockedError, match="agent_safety_blocked"):
        await store.prepare_working_copy(
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=working.id,
            expected_working_copy_version=report.working_copy_row_version,
            idempotency_key="blocked-semantic-cannot-override",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["archive", "restore", "contract"])
@pytest.mark.parametrize("actor", ["non_owner", "removed_owner"])
async def test_lifecycle_and_audience_writes_require_the_active_owner(tmp_path, operation: str, actor: str) -> None:
    """归档、恢复、受众收缩都只许仍在团队里的 owner 做：非 owner、已被移出团队的 owner 一律拒绝，聚合不变。"""
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())
    actor_user_id = "user_member"
    if actor == "removed_owner":
        actor_user_id = "user_owner"
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE team_members SET status='removed' WHERE user_id='user_owner'")
    with sqlite3.connect(db_path) as connection:
        before = connection.execute("SELECT * FROM team_subagent WHERE id='osa_release'").fetchone()

    with pytest.raises(AgentRevisionForbiddenError, match="agent_owner_required"):
        if operation == "archive":
            await store.archive_agent(team_id="team_release", agent_id="osa_release", actor_user_id=actor_user_id, expected_aggregate_row_version=1)
        elif operation == "restore":
            await store.restore_agent(team_id="team_release", agent_id="osa_release", actor_user_id=actor_user_id, expected_aggregate_row_version=1)
        else:
            await store.contract_audience(
                team_id="team_release",
                agent_id="osa_release",
                actor_user_id=actor_user_id,
                expected_aggregate_row_version=1,
                audience=AgentAudience("private"),
                idempotency_key="contract-denied",
                reason_code="owner_removed_audience",
            )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT * FROM team_subagent WHERE id='osa_release'").fetchone() == before
        assert connection.execute("SELECT COUNT(*) FROM admin_audit_log WHERE target_id='osa_release'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_capability_protocol_block_round_trips_through_revision_publish(tmp_path) -> None:
    from bowhead_core.user_store import UserStore

    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock())
    users = UserStore(str(db_path))

    async def seed_release(definition: dict):
        working = await store.save_working_copy(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            definition=definition,
            listing=_listing(),
            audience=AgentAudience("private"),
            expected_row_version=None,
        )
        return await seed_legacy_publication(
            store,
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            working_copy_id=working.id,
            release_note="Toggle the capability protocol block",
        )

    def definition_hash(revision_id: str) -> str:
        with sqlite3.connect(db_path) as connection:
            return connection.execute("SELECT definition_hash FROM team_subagent_revision WHERE id=?", (revision_id,)).fetchone()[0]

    enabled = await seed_release({**_definition(), "capability_protocol_block": True})
    assert enabled.state == "activated"
    rec = await users.get_team_subagent_by_name("team_release", "release-agent")
    assert rec is not None and rec.capability_protocol_block is True

    disabled = await seed_release(_definition())
    assert disabled.state == "activated"
    rec = await users.get_team_subagent_by_name("team_release", "release-agent")
    assert rec is not None and rec.capability_protocol_block is False
    # The field is part of the frozen definition, so toggling it is a new version.
    assert definition_hash(enabled.revision_id) != definition_hash(disabled.revision_id)

    with pytest.raises(AgentPublicationBlockedError, match="agent_definition_projection_invalid"):
        await seed_release({**_definition(), "capability_protocol_block": "yes"})
    rec = await users.get_team_subagent_by_name("team_release", "release-agent")
    assert rec is not None and rec.capability_protocol_block is False


@pytest.mark.asyncio
async def test_prepare_from_latest_draft_replaces_the_prepared_candidate_in_one_step(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), eval_gate=AgentEvalReleaseGate(), clock=_Clock())
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("First prompt."),
        listing=_listing("Candidate Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    first = await store.prepare_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_working_copy_id=working.id,
        expected_working_copy_version=working.row_version,
        expected_eval_policy_revision_id=None,
        workspace_id="ws_eval",
        idempotency_key="prepare-first",
    )
    changed = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Edited after preparing."),
        listing=_listing("Candidate Agent"),
        audience=AgentAudience("private"),
        expected_row_version=working.row_version,
    )
    prepare = {
        "team_id": "team_release",
        "agent_id": "osa_release",
        "actor_user_id": "user_owner",
        "expected_working_copy_id": changed.id,
        "expected_working_copy_version": changed.row_version,
        "expected_eval_policy_revision_id": first.policy_revision_id,
        "workspace_id": "ws_eval",
    }
    replacement = CandidateReplacement(first.candidate_id, first.candidate_row_version, first.candidate_hash)

    # Without naming the candidate to replace, preparing is refused as before.
    with pytest.raises(AgentRevisionConflictError, match="already_active"):
        await store.prepare_working_copy(**prepare, idempotency_key="no-replace")
    # CI and Coach can't replace the Owner's candidate.
    with pytest.raises(AgentRevisionForbiddenError, match="replace_owner_only"):
        await store.prepare_working_copy(**prepare, idempotency_key="replace-coach", coach_delegation_id="acd_x", replace_candidate=replacement)
    # A stale identity changes nothing: the first candidate stays prepared.
    with pytest.raises(AgentRevisionConflictError, match="agent_release_candidate_version_conflict"):
        await store.prepare_working_copy(
            **prepare,
            idempotency_key="replace-stale",
            replace_candidate=CandidateReplacement(first.candidate_id, first.candidate_row_version + 1, first.candidate_hash),
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM team_subagent_release_candidate WHERE id=?", (first.candidate_id,)).fetchone() == ("prepared",)

    second = await store.prepare_working_copy(**prepare, idempotency_key="replace", replace_candidate=replacement)
    replay = await store.prepare_working_copy(**prepare, idempotency_key="replace", replace_candidate=replacement)
    assert replay == second
    assert second.candidate_id != first.candidate_id
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,decision_reason_code FROM team_subagent_release_candidate WHERE id=?", (first.candidate_id,)
        ).fetchone() == ("withdrawn", "owner_replaced")
        assert connection.execute("SELECT state FROM team_subagent_release_candidate WHERE id=?", (second.candidate_id,)).fetchone() == ("prepared",)
        withdrawn = [
            json.loads(row[0])
            for row in connection.execute("SELECT payload_json FROM admin_audit_log WHERE action=?", (AuditAction.AGENT_RELEASE_CANDIDATE_WITHDRAWN,))
        ]
        prepared = [
            json.loads(row[0])
            for row in connection.execute("SELECT payload_json FROM admin_audit_log WHERE action=?", (AuditAction.AGENT_RELEASE_CANDIDATE_PREPARED,))
        ]
        # The Owner isn't notified of their own replacement.
        assert connection.execute(
            "SELECT COUNT(*) FROM team_subagent_notification_outbox WHERE notification_type='agent.release.withdrawn'"
        ).fetchone() == (0,)
    assert withdrawn == [{"candidate_id": first.candidate_id, "reason_code": "owner_replaced"}]
    assert [item.get("replaced_candidate_id") for item in prepared if item["candidate_id"] == second.candidate_id] == [first.candidate_id]

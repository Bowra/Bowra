"""Stage 0A Subagent write-ingress inventory contract tests."""

from __future__ import annotations

import json
from pathlib import Path

from bowhead_cli.main import app
from bowhead_cli.subagent_write_ingress_spike import (
    audit_subagent_write_ingress,
)
from typer.testing import CliRunner

runner = CliRunner()


def test_repository_inventory_is_complete_and_freezes_current_baseline() -> None:
    report = audit_subagent_write_ingress()

    assert all(report.checks.values())
    assert report.schema_version == 2
    # Baseline +1 (40→41 / mutator 25→26): the conversational draft creation route `team_subagent_drafts.
    # create_from_draft` is a new write entry point using the same canonical
    # `TeamSubagentStore.create_team_subagent` transaction as the direct route, with the additional require_active_owner
    # and draft-consumption CAS (source_draft_id / expected_version / definition_fingerprint),
    # so blocking=0 is unchanged. The baseline itself requires deliberate registration of every new write entry point.
    # Baseline +7 (41→48 / SQL 15→21 / mutator 26→27): M2 versioned-aggregate archiving, restoration,
    # audience contraction, live projection, pointer attachment during baseline import, and delegated creation for same-team Fork. The first three each
    # hold BEGIN IMMEDIATE and recheck the owner; the two `_conn` primitives are called only by lock-holding callers; Fork uses
    # canonical `create_team_subagent` and rechecks the source and fork policy under the lock. AgentRevisionStore
    # also becomes the second legitimate transaction-token owner.
    # Baseline +3 (48→51 / SQL 21→23 / mutator 27→28): access-request approval appends one member to the
    # Owner-managed Subagent audience through `TeamSubagentStore.add_team_subagent_audience_user` (BEGIN
    # IMMEDIATE + Owner recheck) and its token-guarded `_append_team_subagent_audience_user_conn` primitive;
    # the report/team_skill grant call sits behind the stable Subagent rejection.
    # Baseline -1 (51→50 / mutator 28→27): the retired report-only access-request decide handler and its
    # direct report grant were removed; the remaining access-request grant goes through `_apply_grant`.
    # Baseline +2 (50→52 / SQL 23→24 / mutator 27→28): account retirement (`retire_account_conn`) quarantines
    # the retiring Owner's private Agents through `prepare_member_deactivation_conn` and revokes every grant to
    # the retired user, both inside the caller's BEGIN IMMEDIATE.
    assert report.summary["relevant_findings"] == 52
    assert report.summary["direct_sql_findings"] == 24
    assert report.summary["mutator_call_findings"] == 28
    assert report.summary["blocking_findings"] == 0
    assert report.summary["unclassified_findings"] == 0
    assert report.summary["enforcement_evidence_failures"] == []
    assert report.summary["transaction_token_issuers"] == [
        {"path": "packages/bowhead_core/src/bowhead_core/agent_revision_store.py", "symbol": "AgentRevisionStore.__init__"},
        {"path": "packages/bowhead_core/src/bowhead_core/team_subagent_store.py", "symbol": "TeamSubagentStore.__init__"},
    ]
    assert report.summary["inventory_fingerprint"] == ("b3a4648e6a90cafc56fb6356d7f8defe2f625e395cee97c9a35a188b86b608fa")
    assert report.release_a_readiness == {
        "ready": True,
        "blocking_findings": 0,
        "required_outcome": ("all blockers delegated, rejected, or restricted to typed internal transactions"),
    }


def test_inventory_classifies_all_high_risk_http_and_store_surfaces() -> None:
    report = audit_subagent_write_ingress()
    by_key = {
        (
            finding["path"],
            finding["symbol"],
            finding["target"],
        ): finding
        for finding in report.findings
    }

    assert (
        by_key[
            (
                "packages/bowhead_core/src/bowhead_core/user_store.py",
                "UserStore.update_team_subagent_meta",
                "update_team_subagent_meta",
            )
        ]["release_a_disposition"]
        == "delegated_to_team_subagent_store"
    )
    assert (
        by_key[
            (
                "packages/bowhead_gateway/src/bowhead_gateway/routes/shares.py",
                "create_share_grant",
                "add_grant",
            )
        ]["release_a_disposition"]
        == "stable_rejection_before_generic_mutator"
    )
    assert (
        by_key[
            (
                "packages/bowhead_gateway/src/bowhead_gateway/routes/access_control.py",
                "upsert_resource_grant",
                "create_or_update_resource_grant",
            )
        ]["release_a_disposition"]
        == "stable_rejection_before_generic_mutator"
    )
    assert (
        by_key[
            (
                "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
                "TeamSubagentStore.prepare_member_deactivation_conn",
                "team_subagent",
            )
        ]["release_a_disposition"]
        == "caller_owned_team_subagent_transaction_primitive"
    )
    assert (
        by_key[
            (
                "packages/bowhead_core/src/bowhead_core/user_store.py",
                "UserStore.admin_soft_delete_member_detailed",
                "prepare_member_deactivation_conn",
            )
        ]["release_a_disposition"]
        == "delegated_to_caller_owned_transaction"
    )
    assert (
        by_key[
            (
                "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
                "TeamSubagentStore.compare_and_update_team_subagent",
                "team_subagent",
            )
        ]["release_a_disposition"]
        == "canonical_team_subagent_transaction"
    )
    assert (
        by_key[
            (
                "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
                "put_subagent_audience",
                "replace_team_subagent_audience",
            )
        ]["release_a_disposition"]
        == "delegated_to_team_subagent_store"
    )
    assert (
        by_key[
            (
                "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
                "patch_subagent_configuration",
                "_mutate_subagent",
            )
        ]["release_a_disposition"]
        == "delegated_to_versioned_team_subagent_mutation"
    )
    assert (
        by_key[
            (
                "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
                "TeamSubagentStore.delete_team_subagent",
                "team_subagent",
            )
        ]["release_a_disposition"]
        == "canonical_team_subagent_transaction"
    )


def test_inventory_proves_report_and_artifact_calls_are_not_subagent_ingress() -> None:
    report = audit_subagent_write_ingress()
    proven = {(finding["path"], finding["symbol"]) for finding in report.findings if finding["release_a_disposition"] == "proven_non_subagent_scope"}

    assert proven == {
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/files.py",
            "share_file",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/files.py",
            "unshare_file",
        ),
    }
    assert report.non_subagent_mutator_calls >= 7


def test_unknown_direct_sql_writer_fails_inventory_closed(tmp_path: Path) -> None:
    source = tmp_path / "packages/example/src/example/bypass.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
def bypass(connection):
    connection.execute("UPDATE team_subagent SET status='enabled'")
""",
        encoding="utf-8",
    )

    report = audit_subagent_write_ingress(tmp_path)

    assert report.summary["unclassified_findings"] == 1
    assert report.checks["all_relevant_findings_classified"] is False
    assert report.checks["release_a_dispositions_are_explicit"] is False
    assert report.findings[0]["symbol"] == "bypass"
    assert report.findings[0]["release_a_disposition"] == "unclassified"
    assert report.findings[0]["blocking"] is True


def test_unknown_generic_grant_call_fails_inventory_closed(tmp_path: Path) -> None:
    source = tmp_path / "packages/example/src/example/bypass.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
async def bypass(access_store, resource_kind):
    await access_store.create_or_update_resource_grant(
        resource_kind=resource_kind,
    )
""",
        encoding="utf-8",
    )

    report = audit_subagent_write_ingress(tmp_path)

    assert report.summary["unclassified_findings"] == 1
    assert report.findings[0]["finding_kind"] == "mutator_call"
    assert report.findings[0]["subagent_scope"] == "potential_subagent"
    assert report.findings[0]["classified"] is False


def test_known_generic_route_without_stable_rejection_fails_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "packages/bowhead_gateway/src/bowhead_gateway/routes/shares.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
async def put_audience(service, kind):
    await service.set_audience(kind=kind)
""",
        encoding="utf-8",
    )

    report = audit_subagent_write_ingress(tmp_path)

    assert report.summary["unclassified_findings"] == 1
    assert report.summary["enforcement_evidence_failures"] == [
        ("packages/bowhead_gateway/src/bowhead_gateway/routes/shares.py:put_audience:set_audience:stable_rejection")
    ]
    assert report.checks["enforcement_evidence_is_present"] is False


def test_token_guard_disposition_requires_source_evidence(tmp_path: Path) -> None:
    source = tmp_path / "packages/bowhead_core/src/bowhead_core/access_control.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
class AccessControlStore:
    async def create_or_update_resource_grant(self, db):
        await db.execute("UPDATE resource_grants SET updated_at='x'")
""",
        encoding="utf-8",
    )

    report = audit_subagent_write_ingress(tmp_path)

    assert report.summary["unclassified_findings"] == 1
    assert report.summary["enforcement_evidence_failures"] == [
        (
            "packages/bowhead_core/src/bowhead_core/access_control.py:"
            "AccessControlStore.create_or_update_resource_grant:"
            "resource_grants:update:transaction_token"
        )
    ]
    assert report.findings[0]["blocking"] is True


def test_caller_owned_transaction_disposition_requires_begin_immediate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "packages/bowhead_core/src/bowhead_core/user_store.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
class UserStore:
    async def admin_soft_delete_member_detailed(self, store, db):
        await store.prepare_member_deactivation_conn(db)
""",
        encoding="utf-8",
    )

    report = audit_subagent_write_ingress(tmp_path)

    assert report.summary["unclassified_findings"] == 1
    assert report.summary["enforcement_evidence_failures"] == [
        (
            "packages/bowhead_core/src/bowhead_core/user_store.py:"
            "UserStore.admin_soft_delete_member_detailed:"
            "prepare_member_deactivation_conn:begin_immediate"
        )
    ]
    assert report.findings[0]["blocking"] is True


def test_private_transaction_token_import_outside_store_blocks_readiness(
    tmp_path: Path,
) -> None:
    source = tmp_path / "packages/example/src/example/bypass.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
from bowhead_core.subagent_write_boundary import _TRANSACTION_TOKEN
""",
        encoding="utf-8",
    )

    report = audit_subagent_write_ingress(tmp_path)

    assert report.summary["unclassified_findings"] == 0
    assert report.checks["transaction_token_private_import_is_confined"] is False
    assert report.release_a_readiness["ready"] is False


def test_private_transaction_token_attribute_reference_blocks_readiness(
    tmp_path: Path,
) -> None:
    source = tmp_path / "packages/example/src/example/bypass.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
import bowhead_core.subagent_write_boundary as boundary

token = boundary._TRANSACTION_TOKEN
""",
        encoding="utf-8",
    )

    report = audit_subagent_write_ingress(tmp_path)

    assert report.checks["transaction_token_private_attribute_is_confined"] is False
    assert report.release_a_readiness["ready"] is False


def test_literal_non_subagent_call_is_not_a_false_positive(tmp_path: Path) -> None:
    source = tmp_path / "packages/example/src/example/report_delete.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
async def delete_report(access_store, team_id, report_id):
    await access_store.revoke_grants_for_resource(
        team_id,
        "report",
        report_id,
    )
""",
        encoding="utf-8",
    )

    report = audit_subagent_write_ingress(tmp_path)

    assert report.findings == []
    assert report.non_subagent_mutator_calls == 1
    assert report.summary["unclassified_findings"] == 0
    assert report.checks["all_relevant_findings_classified"] is True


def test_inventory_is_deterministic_and_excludes_only_named_synthetic_sources() -> None:
    first = audit_subagent_write_ingress()
    second = audit_subagent_write_ingress()

    assert first.to_dict() == second.to_dict()
    assert first.excluded_synthetic_sources == [
        "packages/bowhead_cli/src/bowhead_cli/subagent_access_spike.py",
        "packages/bowhead_cli/src/bowhead_cli/subagent_benchmark.py",
        "packages/bowhead_cli/src/bowhead_cli/subagent_dependency_spike.py",
        "packages/bowhead_cli/src/bowhead_cli/subagent_trigger_spike.py",
        "packages/bowhead_cli/src/bowhead_cli/subagent_write_ingress_spike.py",
    ]
    assert first.source_state == "working_tree_including_uncommitted_files"
    assert first.methodology["production_mutation"] == "none"


def test_cli_json_emits_safe_structured_inventory() -> None:
    result = runner.invoke(
        app,
        ["admin", "audit-subagent-write-ingress", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scope"] == "repository_static_write_ingress_inventory"
    assert all(payload["checks"].values())
    assert payload["release_a_readiness"]["ready"] is True
    assert payload["summary"]["blocking_findings"] == 0
    assert "Synthetic prompt" not in result.output
    assert "system_prompt =" not in result.output


def test_transaction_token_attribute_read_outside_owner_class_blocks_readiness(tmp_path: Path) -> None:
    source = tmp_path / "packages/bowhead_core/src/bowhead_core/agent_version_service.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
class AgentVersionService:
    async def leak(self, db):
        await self._revision_store._access_store._revoke_grants_for_resource_conn(
            db, "t", "team_subagent", "a", subagent_transaction_token=self._revision_store._transaction_token
        )
""",
        encoding="utf-8",
    )

    report = audit_subagent_write_ingress(tmp_path)

    assert report.checks["transaction_token_instance_attribute_is_confined"] is False
    assert report.summary["transaction_token_attribute_escapes"] == [
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.leak:5"
    ]
    assert report.checks["revision_store_private_access_is_registered"] is False
    assert (
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.leak:_access_store"
        in (report.summary["unregistered_revision_store_private_access"])
    )
    assert report.release_a_readiness["ready"] is False

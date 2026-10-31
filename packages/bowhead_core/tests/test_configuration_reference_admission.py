"""Configuration references preserve the real installed Agent admission path."""

from __future__ import annotations

import copy

import pytest
from bowhead_core.chat_reference_preparation import _only_manifest_reference_grants
from bowhead_core.chat_references import AgentResourceRef
from bowhead_core.skill_read_resolution import build_reference_skill_sources
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from test_reference_primary_reader import _read
from test_system_reference_start_preparation import accept, prepare
from test_system_reference_start_preparation import start_case as _start_case
from test_system_reference_start_preparation import system_case as _system_case

start_case = _start_case
system_case = _system_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["system"], indirect=True),
    pytest.mark.parametrize("system_case", ["startup"], indirect=True),
]


async def test_configuration_reference_accepts_the_installed_profile(system_case):
    case = system_case
    projected = await case.runner.project_system_behavior_candidate(profile_name="configuration-agent", parent_ctx=case.ctx)
    document = case.args["reference_start"].input_document
    document.nodes[0] = document.nodes[0].model_copy(
        update={
            "label": "Configuration Agent",
            "resource": AgentResourceRef(kind="agent", source_kind="system", id="system:configuration-agent", version=projected.revision_ref),
        }
    )
    index = build_builtin_skill_metadata()
    sources = build_reference_skill_sources(case.fixture.db_path, scope=case.request_scope, builtin_index=lambda: index)
    admission = await prepare(case, input_document=document, sources=sources)
    assert admission.skill_sources is None and admission.required_skill_modes == ()
    assert admission.system_revision.definition == projected.definition
    await accept(case, admission)
    assert (await _read(case)).binding.revision_ref == projected.revision_ref


@pytest.mark.parametrize("changed_field", ["revision", "permissions", "missing_grant"])
async def test_on_demand_configuration_read_does_not_admit_changed_grants(system_case, changed_field):
    projected = await system_case.runner.project_system_behavior_candidate(profile_name="configuration-agent", parent_ctx=system_case.ctx)
    definition = copy.deepcopy(projected.definition)
    assert _only_manifest_reference_grants("configuration-agent", definition)
    if changed_field == "missing_grant":
        definition["skill_grants"].pop()
    elif changed_field == "permissions":
        definition["skill_grants"][0][changed_field] = ["read", "execute"]
    else:
        definition["skill_grants"][0][changed_field] = "sha256:" + "0" * 64
    assert not _only_manifest_reference_grants("configuration-agent", definition)

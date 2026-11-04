"""Explicit method text is task input, without changing the Agent's grants."""

import json
import sqlite3
from uuid import uuid4

import pytest
import respx
from bowhead_core.chat_references import InputDocument
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from test_reference_http_start import answer_response, payload, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_skill_read_receiver_integration import package

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
]


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("receiver_kind", ["team", "ordinary_system", "production_system"])
async def test_unconfigured_standalone_method_reaches_the_exact_agent_without_assignment(http_start, receiver_kind):
    case = http_start
    name = "temporary_method"
    method = package(case.fixture.root, name, ordinary=True, executable=True)
    index = build_builtin_skill_metadata(builtin_root=case.fixture.root)
    case.fixture.sources.builtin_index = lambda: index
    case.app.state.builtin_skill_reference_metadata = index
    await case.fixture.sources.prepare(method, scope=case.fixture.scope)
    body = payload(case, skills=False)
    agent = next(node["resource"] for node in body["input_document"]["nodes"] if node["type"] == "reference")
    if receiver_kind in {"ordinary_system", "production_system"}:
        from types import SimpleNamespace

        from bowhead_core.system_agent_reference_metadata import build_system_agent_reference_metadata

        if receiver_kind == "production_system":
            from bowhead_core.agents.registry import AgentRegistry

            case.app.state.orchestrator.subagent_runner._registry = AgentRegistry.bootstrap()
        index_system = await build_system_agent_reference_metadata(case.path, case.app.state.orchestrator.subagent_runner)
        case.app.state.system_agent_reference_metadata = SimpleNamespace(current=lambda: index_system)
        discovered = await case.client.post(
            "/api/v1/chat/references/catalog",
            headers={"Content-Type": "application/json"},
            json={"workspace_id": case.fixture.scope.workspace_id, "trigger": "@"},
        )
        system = [item for group in discovered.json()["groups"] if group["id"] == "system_agents" for item in group["items"]]
        agent = next(item["resource"] for item in system if item["id"] == "system:compute")
        next(node for node in body["input_document"]["nodes"] if node["type"] == "reference")["resource"] = agent
    else:
        with sqlite3.connect(case.path) as db:
            (definition_before,) = db.execute("SELECT definition_json FROM team_subagent_revision WHERE id=?", (agent["version"],)).fetchone()
        if case.fixture.mode == "admission":
            assert "runtime_extensions" not in json.loads(definition_before)
    catalog = await case.client.post(
        "/api/v1/chat/references/catalog",
        headers={"Content-Type": "application/json"},
        json={"workspace_id": case.fixture.scope.workspace_id, "trigger": "/", "query": name, "selected_refs": [agent]},
    )
    assert catalog.status_code == 200, catalog.text
    (item,) = [item for group in catalog.json()["groups"] for item in group["items"] if item.get("resource") == method.model_dump(mode="json")]
    assert item["availability"] == "readonly_extension" and item["reason_code"] is None
    reverse = await case.client.post(
        "/api/v1/chat/references/catalog",
        headers={"Content-Type": "application/json"},
        json={"workspace_id": case.fixture.scope.workspace_id, "trigger": "@", "selected_refs": [item["resource"]]},
    )
    receiver_item = next(i for g in reverse.json()["groups"] for i in g["items"] if i.get("resource") == agent)
    assert receiver_item["availability"] == "baseline"
    body["input_document"]["nodes"].append(
        {"type": "reference", "node_id": str(uuid4()), "resource": item["resource"], "label": item["label"], "parameters": {}}
    )
    body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return answer_response(request)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=body)
        assert accepted.status_code == 200, accepted.text
        await settled(accepted.json()["turn_id"])
        duplicate = await case.client.post("/api/v1/chat/send", json=body)
        assert duplicate.status_code == 200 and duplicate.json()["turn_id"] == accepted.json()["turn_id"]
    turn = await case.fixture.case.store.get_turn(accepted.json()["turn_id"])
    assert turn.status == "completed", turn.error_kind
    assert len(requests) == 1
    assert "BASELINE_SOP_" + name in json.dumps(requests)
    assert "fixture script must never run" not in json.dumps(requests)
    (execution,) = rows(case, "turn_agent_execution")
    assert (execution["agent_id"], execution["revision_ref"]) == (agent["id"], agent["version"])
    if receiver_kind == "production_system":
        with sqlite3.connect(case.path) as db:
            (raw,) = db.execute("SELECT descriptor_json FROM system_agent_revision_behavior WHERE revision_id=?", (agent["version"],)).fetchone()
        assert json.loads(raw)["verification_state"] == "blocked"
    if receiver_kind == "team":
        with sqlite3.connect(case.path) as db:
            (definition_after,) = db.execute("SELECT definition_json FROM team_subagent_revision WHERE id=?", (agent["version"],)).fetchone()
        assert definition_after == definition_before


def standalone_request(case, *, ordinary=True, children=()):
    method = package(case.fixture.root, "temporary_method", children, ordinary=ordinary, executable=True)
    index = build_builtin_skill_metadata(builtin_root=case.fixture.root)
    case.fixture.sources.builtin_index = lambda: index
    case.app.state.builtin_skill_reference_metadata = index
    body = payload(case, skills=False)
    body["input_document"]["nodes"].append(
        {"type": "reference", "node_id": str(uuid4()), "resource": method.model_dump(mode="json"), "label": "Temporary method", "parameters": {}}
    )
    body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
    return method, body


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_temporary_method_without_mention_uses_the_existing_receiver_selection(http_start):
    case = http_start
    method, body = standalone_request(case)
    body["input_document"]["nodes"] = [node for node in body["input_document"]["nodes"] if node.get("resource", {}).get("kind") != "agent"]
    body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
    catalog = await case.client.post(
        "/api/v1/chat/references/catalog",
        headers={"Content-Type": "application/json"},
        json={"workspace_id": case.fixture.scope.workspace_id, "trigger": "/", "query": "temporary_method"},
    )
    item = next(item for group in catalog.json()["groups"] for item in group["items"] if item.get("resource") == method.model_dump(mode="json"))
    assert item["availability"] == "readonly_extension"
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post("/api/v1/chat/send", json=body)
        assert response.status_code == 200, response.text
        await settled(response.json()["turn_id"])
    assert (await case.fixture.case.store.get_turn(response.json()["turn_id"])).status == "completed"
    (binding,) = rows(case, "turn_agent_binding")
    assert binding["binding_source"] == "lead"
    assert "BASELINE_SOP_temporary_method" in json.dumps(requests)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_lead_delegation_preserves_ordinary_system_authority_with_temporary_method(http_start, monkeypatch):
    from types import SimpleNamespace

    from bowhead_core.system_agent_reference_metadata import build_system_agent_reference_metadata
    from test_reference_http_start import enable_lead_writer
    from test_skill_extension_runtime import tool_response

    case = http_start
    enable_lead_writer(case, monkeypatch)
    from bowhead_core.agents.registry import AgentRegistry

    case.app.state.orchestrator.subagent_runner._registry = AgentRegistry.bootstrap()
    _, body = standalone_request(case)
    system_index = await build_system_agent_reference_metadata(case.path, case.app.state.orchestrator.subagent_runner)
    case.app.state.system_agent_reference_metadata = SimpleNamespace(current=lambda: system_index)
    catalog = await case.client.post(
        "/api/v1/chat/references/catalog",
        headers={"Content-Type": "application/json"},
        json={"workspace_id": case.fixture.scope.workspace_id, "trigger": "@"},
    )
    system = [item for group in catalog.json()["groups"] if group["id"] == "system_agents" for item in group["items"]]
    agent = next(item for item in system if item["id"] == "system:compute")
    next(node for node in body["input_document"]["nodes"] if node.get("resource", {}).get("kind") == "agent")["resource"] = agent["resource"]
    body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
    requests = []

    def respond(request):
        body_request = json.loads(request.content)
        requests.append(body_request)
        if len(requests) == 1:
            return tool_response(
                "subagent_spawn",
                {"profile": "compute", "task": "Apply the selected method", "delegation_role": "primary", "required_source_urls": []},
                stream=body_request.get("stream", False),
            )
        return answer_response(request)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post("/api/v1/chat/send", json=body)
        assert response.status_code == 200, response.text
        await settled(response.json()["turn_id"])
    assert (await case.fixture.case.store.get_turn(response.json()["turn_id"])).status == "completed"
    assert len(requests) >= 2 and "BASELINE_SOP_temporary_method" in json.dumps(requests[1])


@pytest.mark.parametrize("receiver_case", ["admission_extension_wildcard"], indirect=True)
async def test_plain_method_can_use_task_authority_when_an_older_published_selector_also_matches(http_start):
    import yaml
    from bowhead_core.skill_upload import bundle_sha256
    from bowhead_skill.loader import split_skill_markdown

    case = http_start
    path = case.fixture.root / "optional_method" / "SKILL.md"
    header, _ = split_skill_markdown(path.read_text())
    metadata = yaml.safe_load(header)
    del metadata["read_only_entry"]
    path.write_text("---\n" + yaml.safe_dump(metadata) + "---\nPLAIN_METHOD_BODY")
    method = case.fixture.optional.model_copy(update={"version": "sha256:" + bundle_sha256(path.parent)})
    index = build_builtin_skill_metadata(builtin_root=case.fixture.root)
    case.fixture.sources.builtin_index = lambda: index
    case.app.state.builtin_skill_reference_metadata = index
    body = payload(case, skills=False)
    body["input_document"]["nodes"].append(
        {"type": "reference", "node_id": str(uuid4()), "resource": method.model_dump(mode="json"), "label": "Method", "parameters": {}}
    )
    body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
    agent = next(node["resource"] for node in body["input_document"]["nodes"] if node.get("resource", {}).get("kind") == "agent")
    catalog = await case.client.post(
        "/api/v1/chat/references/catalog",
        headers={"Content-Type": "application/json"},
        json={"workspace_id": case.fixture.scope.workspace_id, "trigger": "/", "query": "optional_method", "selected_refs": [agent]},
    )
    item = next(item for group in catalog.json()["groups"] for item in group["items"] if item.get("resource") == method.model_dump(mode="json"))
    assert item["availability"] == "readonly_extension"
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post("/api/v1/chat/send", json=body)
        assert response.status_code == 200, response.text
        await settled(response.json()["turn_id"])
    assert (await case.fixture.case.store.get_turn(response.json()["turn_id"])).status == "completed"
    assert "PLAIN_METHOD_BODY" in json.dumps(requests)


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_temporary_method_does_not_grant_scripts_and_preserves_the_original_invoke(http_start, monkeypatch):
    from types import SimpleNamespace

    from test_skill_extension_runtime import tool_response

    from bowhead_skill import SkillRuntime

    case = http_start
    _, body = standalone_request(case)
    calls, requests = [], []

    async def run(runtime, spec, **kwargs):
        calls.append(spec.name)
        return SimpleNamespace(
            ok=True, exit_code=0, stdout="CALCULATED 42", stderr="", result_json={"answer": 42}, outputs=[], workdir=None, duration_ms=1, error=None
        )

    monkeypatch.setattr(SkillRuntime, "run", run)

    def respond(request):
        sent = json.loads(request.content)
        requests.append(sent)
        if len(requests) <= 2:
            name = "temporary_method" if len(requests) == 1 else "unused"
            return tool_response("skill_invoke", {"name": name, "script": "runner.py", "input": {}}, stream=sent.get("stream", False))
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=body)
        assert accepted.status_code == 200, accepted.text
        await settled(accepted.json()["turn_id"])
    assert (await case.fixture.case.store.get_turn(accepted.json()["turn_id"])).status == "completed"
    assert calls == ["unused"]
    assert len(requests) == 3
    assert "reading only" in json.dumps(requests[1])
    assert "CALCULATED 42" in json.dumps(requests[2])
    assert all(json.loads(row["policy_check_json"])["schema_version"] == 3 for row in rows(case, "chat_reference_extension_checks"))


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("change", ["revoked", "stale_version", "implicit_dependency", "missing_guard", "wrong_space"])
async def test_temporary_method_cannot_bypass_existing_source_and_admission_fences(http_start, monkeypatch, change):
    from test_reference_http_start import acceptance_counts, deny_skill

    case = http_start
    children = (package(case.fixture.root, "unselected_child"),) if change == "implicit_dependency" else ()
    method, body = standalone_request(case, ordinary=not children, children=children)
    if change == "revoked":
        monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "0")
        deny_skill(case, "temporary_method")
    elif change == "stale_version":
        body["input_document"]["nodes"][-1]["resource"]["version"] = "sha256:" + "f" * 64
    elif change == "missing_guard":
        with sqlite3.connect(case.path) as db:
            db.execute("DROP TRIGGER trg_reference_method_composition")
    elif change == "wrong_space":
        body["workspace_id"] = "ws_unavailable"
    before = acceptance_counts(case)
    response = await case.client.post("/api/v1/chat/send", json=body)
    assert response.status_code in {403, 404, 409}, response.text
    assert acceptance_counts(case) == before
    assert not rows(case, "chat_reference_model_invocations")


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_automatic_selection_uses_the_same_production_descriptor_eligibility(http_start):
    from types import SimpleNamespace

    import httpx2
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.system_agent_reference_metadata import build_system_agent_reference_metadata

    case = http_start
    from bowhead_router import AgentBinding

    case.app.state.orchestrator.router.bind(AgentBinding(agent_type="lead", primary=("primary", "large")))
    case.app.state.orchestrator.router.bind(AgentBinding(agent_type="compute", primary=("primary", "large")))
    _, body = standalone_request(case)
    body["input_document"]["nodes"] = [n for n in body["input_document"]["nodes"] if n.get("resource", {}).get("kind") != "agent"]
    body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
    registry = AgentRegistry.bootstrap()
    case.app.state.orchestrator.multi_agent_registry = registry
    case.app.state.orchestrator.subagent_runner._registry = registry
    system_index = await build_system_agent_reference_metadata(case.path, case.app.state.orchestrator.subagent_runner)
    case.app.state.system_agent_reference_metadata = SimpleNamespace(current=lambda: system_index)
    catalog = await case.client.post(
        "/api/v1/chat/references/catalog",
        headers={"Content-Type": "application/json"},
        json={"workspace_id": case.fixture.scope.workspace_id, "trigger": "@"},
    )
    agent = next(item for group in catalog.json()["groups"] for item in group["items"] if item.get("id") == "system:compute")
    requests = []

    def respond(request):
        sent = json.loads(request.content)
        requests.append(sent)
        if len(requests) == 1:
            choices = json.loads(sent["messages"][-1]["content"])["candidates"]
            selected = next(item["candidate_index"] for item in choices if item["name"] == agent["label"])
            return httpx2.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": json.dumps({"candidate_index": selected})}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5},
                },
            )
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=body)
        assert accepted.status_code == 200, accepted.text
        await settled(accepted.json()["turn_id"])
    turn = await case.fixture.case.store.get_turn(accepted.json()["turn_id"])
    assert turn.status == "completed", turn.error_kind
    assert len(requests) >= 2 and "BASELINE_SOP_temporary_method" in json.dumps(requests[1])
    assert len(rows(case, "chat_reference_model_invocations")) == 1
    (binding,) = rows(case, "turn_agent_binding")
    assert binding["binding_source"] == "lead" and binding["requested_agent_id"] == "system:compute"


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_temporary_method_cannot_hide_a_missing_original_baseline_package(http_start):
    import shutil

    from test_reference_http_start import acceptance_counts

    case = http_start
    method, body = standalone_request(case)
    agent = next(n["resource"] for n in body["input_document"]["nodes"] if n.get("resource", {}).get("kind") == "agent")
    shutil.rmtree(case.fixture.root / "unused")
    index = build_builtin_skill_metadata(builtin_root=case.fixture.root)
    case.fixture.sources.builtin_index = lambda: index
    case.app.state.builtin_skill_reference_metadata = index
    catalog = await case.client.post(
        "/api/v1/chat/references/catalog",
        headers={"Content-Type": "application/json"},
        json={"workspace_id": case.fixture.scope.workspace_id, "trigger": "/", "query": "temporary_method", "selected_refs": [agent]},
    )
    item = next(item for group in catalog.json()["groups"] for item in group["items"] if item.get("resource") == method.model_dump(mode="json"))
    assert item["availability"] == "unavailable"
    before = acceptance_counts(case)
    denied = await case.client.post("/api/v1/chat/send", json=body)
    assert denied.status_code == 409
    assert acceptance_counts(case) == before
    assert not rows(case, "chat_reference_model_invocations")


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_production_system_method_keeps_its_descriptor_and_original_question_resume(http_start, monkeypatch):
    from types import SimpleNamespace

    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.system_agent_reference_metadata import build_system_agent_reference_metadata
    from test_reference_http_start import test_lead_delegation_http_question_resumes_the_original_execution as original_question_flow

    case = http_start
    _, body = standalone_request(case)
    case.app.state.orchestrator.subagent_runner._registry = AgentRegistry.bootstrap()
    index = await build_system_agent_reference_metadata(case.path, case.app.state.orchestrator.subagent_runner)
    case.app.state.system_agent_reference_metadata = SimpleNamespace(current=lambda: index)
    catalog = await case.client.post(
        "/api/v1/chat/references/catalog",
        headers={"Content-Type": "application/json"},
        json={"workspace_id": case.fixture.scope.workspace_id, "trigger": "@"},
    )
    resource = next(item["resource"] for group in catalog.json()["groups"] for item in group["items"] if item.get("id") == "system:compute")
    next(n for n in body["input_document"]["nodes"] if n.get("resource", {}).get("kind") == "agent")["resource"] = resource
    body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
    await original_question_flow(
        case, monkeypatch, decision="answered", skills=True, source_kind="system", selected_payload=body, selected_profile="compute"
    )
    with sqlite3.connect(case.path) as db:
        (raw,) = db.execute("SELECT descriptor_json FROM system_agent_revision_behavior WHERE revision_id=?", (resource["version"],)).fetchone()
    assert json.loads(raw)["verification_state"] == "blocked"

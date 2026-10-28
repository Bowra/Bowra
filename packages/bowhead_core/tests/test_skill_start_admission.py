"""Mixed reference preflight and actual submission finalization, without a worker."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import anyio
import pytest
from bowhead_core.chat_reference_admission import prepare_skill_reference_start_admission
from bowhead_core.chat_references import InputDocument, ReferenceNode
from bowhead_core.skill_read_closure import SkillReadClosureError, snapshot_prepared_skill_read_closure
from bowhead_core.skill_read_material import SkillReadMaterialError
from bowhead_core.skill_read_primary import prepare_reference_primary_skill_reads
from bowhead_core.skill_read_receiver import SkillReadReceiverError
from bowhead_core.skill_read_render import SkillReadRenderError
from bowhead_core.skill_read_resolution import SkillReadResolutionError
from test_reference_start_admission import _assert_no_acceptance
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

receiver_case = _receiver_case
start_case = _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
]


async def candidate(fixture, *, agent_first=False, implicit=False):
    case = fixture.case
    admission = case.args["reference_start"]
    methods = [
        ReferenceNode(type="reference", node_id=str(uuid4()), resource=resource, label="METHOD", parameters={} if implicit else {"year": year})
        for resource, year in (((fixture.selected, 2026),) if implicit else ((fixture.selected, 2026), (fixture.child, 2025)))
    ]
    nodes = [*admission.input_document.nodes, *methods] if agent_first else [*methods, *admission.input_document.nodes]
    document = InputDocument.model_validate({"schema_version": 1, "nodes": [node.model_dump(mode="python") for node in nodes]})
    admission = replace(admission, input_document=document)
    prepared = await prepare_skill_reference_start_admission(admission, sources=fixture.sources, scope=fixture.scope)
    case.args.update(reference_start=prepared, user_text=document.display_text())
    return prepared


def counts(case):
    with sqlite3.connect(case.fixture.db_path) as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE turn_id='t_reference'").fetchone()[0]
            for table in (
                "turn_agent_execution",
                "turn_agent_binding",
                "chat_reference_plans",
                "chat_submission_receipts",
                "chat_turn_spawn_dispatch",
                "chat_reference_resolutions",
                "chat_reference_content_snapshots",
            )
        }


@pytest.mark.parametrize("agent_first", [False, True])
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_mixed_input_finalizes_one_primary_and_retains_all_skill_parameters(receiver_case, agent_first):
    fixture = receiver_case
    prepared = await candidate(fixture, agent_first=agent_first)
    receipt = await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    assert receipt.outcome == "started"
    assert counts(fixture.case) == {
        "turn_agent_execution": 1,
        "turn_agent_binding": 1,
        "chat_reference_plans": 1,
        "chat_submission_receipts": 1,
        "chat_turn_spawn_dispatch": 1,
        "chat_reference_resolutions": 0,
        "chat_reference_content_snapshots": 0,
    }
    actual_scope = fixture.scope.model_copy(update={"session_id": "s_reference"})
    receiver, rendered = await prepare_reference_primary_skill_reads(fixture.sources, scope=actual_scope, turn_id="t_reference")
    values = {item["resource"]["id"]: item["parameters"] for item in rendered.document()["skills"]}
    assert values == {"builtin:method": {"year": 2026}, "builtin:support": {"year": 2025}}
    assert receiver.receiver.revision_ref == prepared.expected.revision_ref
    assert [item.proof.resource for item in receiver.closure.items] == [fixture.child, fixture.selected]
    # The initial worker has its own narrower contract until mixed composition
    # and material persistence are wired; accepting metadata cannot bypass it.
    with pytest.raises(ValueError, match="^reference_start_unsupported$"):
        await fixture.reader()
    with sqlite3.connect(fixture.case.fixture.db_path) as db:
        import json

        plan = json.loads(db.execute("SELECT plan_json FROM chat_reference_plans WHERE turn_id='t_reference'").fetchone()[0])
    assert [item["node_id"] for item in plan["admissions"]] == [
        node.node_id for node in prepared.input_document.nodes if isinstance(node, ReferenceNode)
    ]
    assert sorted(item["operation"] for item in plan["admissions"]) == ["delegate", "read", "read"]


@pytest.mark.parametrize("change", ["disabled_root", "disabled_dependency", "scope", "database", "epoch"])
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_changed_preflight_facts_roll_back_every_acceptance_record(receiver_case, change):
    fixture = receiver_case
    prepared = await candidate(fixture)
    if change.startswith("disabled"):
        name = "method" if change == "disabled_root" else "support"
        await fixture.sources.instances.upsert(
            user_id=fixture.scope.user_id, name=name, source_type="builtin", source_path=str(fixture.root / name), enabled=False
        )
    elif change == "scope":
        closure = prepared.skill_reads
        different = closure.scope.model_copy(update={"session_id": "s_different"})
        # Obtain an internally coherent preflight for another already-owned
        # session; it must still not authorize this new-session submission.
        with sqlite3.connect(fixture.case.fixture.db_path) as db:
            db.execute(
                "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at) VALUES(?,?,?,'2026-09-14','2026-09-14')",
                (different.session_id, different.workspace_id, different.user_id),
            )
        other = await prepare_skill_reference_start_admission(prepared, sources=fixture.sources, scope=different)
        fixture.case.args["reference_start"] = other
    elif change == "database":
        fixture.sources.db_path = Path("different-server-database.db")
    else:
        with sqlite3.connect(fixture.case.fixture.db_path) as db:
            db.execute(
                "INSERT INTO access_policy_state(team_id,epoch,updated_at) VALUES(?,1,'2026-09-14') ON CONFLICT(team_id) DO UPDATE SET epoch=epoch+1",
                (fixture.scope.team_id,),
            )
    with pytest.raises((SkillReadResolutionError, ValueError)):
        await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    _assert_no_acceptance(fixture.case)


@pytest.mark.parametrize("receiver_case", ["admission_root_only"], indirect=True)
async def test_dependency_outside_published_baseline_rolls_back_the_reserved_primary(receiver_case):
    await candidate(receiver_case)
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_extension_denied$"):
        await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    _assert_no_acceptance(receiver_case.case)


@pytest.mark.parametrize("receiver_case", ["admission_selected_invoke", "admission_unused_invoke"], indirect=True)
async def test_invocable_baseline_is_accepted_without_widening_selected_reads(receiver_case):
    await candidate(receiver_case)
    result = await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    assert result.outcome == "started"
    actual_scope = receiver_case.scope.model_copy(update={"session_id": "s_reference"})
    prepared, rendered = await prepare_reference_primary_skill_reads(receiver_case.sources, scope=actual_scope, turn_id="t_reference")
    assert any("invoke" in grant.permissions for grant in prepared.receiver.grants)
    assert {item["resource"]["id"] for item in rendered.document()["skills"]} == {"builtin:method", "builtin:support"}


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_invocation_migration_is_required_before_acceptance(receiver_case):
    await candidate(receiver_case)
    with sqlite3.connect(receiver_case.case.fixture.db_path) as db:
        db.execute("DROP TRIGGER trg_reference_skill_invocation_composition")
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_invocation_migration_required$"):
        await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    _assert_no_acceptance(receiver_case.case)


@pytest.mark.parametrize("receiver_case", ["admission_unused_read", "admission_unused_sop"], indirect=True)
async def test_unselected_baseline_is_accepted_without_becoming_a_selected_method(receiver_case):
    await candidate(receiver_case)
    result = await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    assert result.outcome == "started"
    actual_scope = receiver_case.scope.model_copy(update={"session_id": "s_reference"})
    prepared, rendered = await prepare_reference_primary_skill_reads(receiver_case.sources, scope=actual_scope, turn_id="t_reference")
    assert len(prepared.receiver.grants) == len(prepared.baseline.packages) == 3
    assert {item["resource"]["id"] for item in rendered.document()["skills"]} == {"builtin:method", "builtin:support"}


@pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True)
async def test_unselected_baseline_revocation_rolls_back_the_whole_acceptance(receiver_case):
    await candidate(receiver_case)
    await receiver_case.sources.instances.upsert(
        user_id=receiver_case.scope.user_id, name="unused", source_type="builtin", source_path=str(receiver_case.root / "unused"), enabled=False
    )
    with pytest.raises(SkillReadReceiverError):
        await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    _assert_no_acceptance(receiver_case.case)


@pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True)
async def test_unselected_baseline_authority_check_does_not_read_files_in_finalizer(receiver_case, monkeypatch):
    await candidate(receiver_case)

    def forbidden(*args, **kwargs):
        raise AssertionError("file read inside admission")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert (await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)).outcome == "started"


@pytest.mark.parametrize("receiver_case", ["admission_unused_read"], indirect=True)
async def test_selecting_the_additional_read_method_preserves_the_whole_baseline(receiver_case):
    prepared = await candidate(receiver_case)
    document = prepared.input_document.model_copy(
        update={
            "nodes": [
                *prepared.input_document.nodes,
                ReferenceNode(type="reference", node_id=str(uuid4()), resource=receiver_case.unused, label="Additional method", parameters={}),
            ]
        }
    )
    prepared = await prepare_skill_reference_start_admission(
        replace(prepared, input_document=document), sources=receiver_case.sources, scope=receiver_case.scope
    )
    receiver_case.case.args.update(reference_start=prepared, user_text=document.display_text())
    result = await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    assert result.outcome == "started"
    actual_scope = receiver_case.scope.model_copy(update={"session_id": "s_reference"})
    receiver, rendered = await prepare_reference_primary_skill_reads(receiver_case.sources, scope=actual_scope, turn_id="t_reference")
    assert len(receiver.receiver.grants) == 3
    assert {item["resource"]["id"] for item in rendered.document()["skills"]} == {"builtin:method", "builtin:support", "builtin:unused"}


@pytest.mark.parametrize("receiver_case", ["admission_unused_read", "admission_selected_invoke", "admission_unused_invoke"], indirect=True)
async def test_agent_only_admission_does_not_apply_mixed_read_only_restrictions(receiver_case):
    # No Skill selection: retain the separate Agent-only admission contract.
    receipt = await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    assert receipt.outcome == "started"
    primary = await receiver_case.reader()
    import json

    with sqlite3.connect(receiver_case.case.fixture.db_path) as db:
        original = json.loads(
            db.execute("SELECT definition_json FROM team_subagent_revision WHERE id=?", (primary.binding.revision_ref,)).fetchone()[0]
        )
    assert primary.binding.definition["skill_grants"] == original["skill_grants"]


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_existing_receipt_does_not_reauthorize_or_repeat_skill_admission(receiver_case, monkeypatch):
    await candidate(receiver_case)
    first = await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    await receiver_case.sources.instances.upsert(
        user_id=receiver_case.scope.user_id, name="method", source_type="builtin", source_path=str(receiver_case.root / "method"), enabled=False
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("accepted receipt must not reauthorize")

    monkeypatch.setattr(receiver_case.sources, "revalidate_in_connection", forbidden)
    second = await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    assert first == second
    assert counts(receiver_case.case)["turn_agent_execution"] == 1


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_concurrent_mixed_finalizers_share_one_receipt_and_execution(receiver_case):
    await candidate(receiver_case)
    receipts = []

    async def submit():
        receipts.append(await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args))

    async with anyio.create_task_group() as group:
        group.start_soon(submit)
        group.start_soon(submit)
    assert len(receipts) == 2 and receipts[0] == receipts[1]
    assert counts(receiver_case.case)["turn_agent_execution"] == 1


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_transaction_uses_detached_parameters_without_file_io(receiver_case, monkeypatch):
    prepared = await candidate(receiver_case)
    original = receiver_case.sources.revalidate_in_connection
    visits = []

    async def observe(db, proof):
        assert db.in_transaction
        visits.append(proof.resource)
        prepared.input_document.nodes[0].parameters["year"] = 2099
        return await original(db, proof)

    def forbidden(*args, **kwargs):
        raise AssertionError("file IO during admission")

    monkeypatch.setattr(receiver_case.sources, "revalidate_in_connection", observe)
    with monkeypatch.context() as patch:
        for name in ("resolve", "stat", "read_bytes", "read_text", "open"):
            patch.setattr(Path, name, forbidden)
        await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    assert visits == [receiver_case.child, receiver_case.selected]
    _, rendered = await prepare_reference_primary_skill_reads(
        receiver_case.sources, scope=receiver_case.scope.model_copy(update={"session_id": "s_reference"}), turn_id="t_reference"
    )
    assert next(item for item in rendered.document()["skills"] if item["resource"]["id"] == receiver_case.selected.id)["parameters"] == {"year": 2026}


@pytest.mark.parametrize("change", ["parameter", "missing_child", "order", "root", "content", "size", "digest", "extra_duplicate"])
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_corrupted_preparation_never_becomes_an_admission(receiver_case, change):
    prepared = await candidate(receiver_case)
    closure = prepared.skill_reads
    if change == "parameter":
        prepared.input_document.nodes[0].parameters["year"] = "unknown"
    else:
        if change == "missing_child":
            closure = replace(closure, items=closure.items[1:])
        elif change == "order":
            closure = replace(closure, items=tuple(reversed(closure.items)))
        elif change == "root":
            closure = replace(closure, roots=tuple(reversed(closure.roots)))
        elif change == "content":
            item = closure.items[0]
            closure = replace(
                closure, items=(replace(item, material=replace(item.material, content=item.material.content + b" ")), *closure.items[1:])
            )
        elif change == "size":
            closure = replace(closure, text_bytes=closure.text_bytes + 1)
        elif change == "digest":
            closure = replace(closure, closure_digest="sha256:" + "0" * 64)
        else:
            closure = replace(closure, items=(*closure.items, closure.items[0]))
        receiver_case.case.args["reference_start"] = replace(prepared, skill_reads=closure)
    with pytest.raises((SkillReadClosureError, SkillReadMaterialError, SkillReadRenderError)):
        await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    _assert_no_acceptance(receiver_case.case)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_closure_snapshot_round_trip_preserves_prepared_graph(receiver_case):
    prepared = await candidate(receiver_case)
    snapshot = snapshot_prepared_skill_read_closure(prepared.skill_reads)
    assert snapshot == prepared.skill_reads and snapshot is not prepared.skill_reads
    assert snapshot.items[0] is not prepared.skill_reads.items[0]
    assert snapshot.items[0].material.content is prepared.skill_reads.items[0].material.content


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_existing_owned_session_keeps_its_preflight_scope(receiver_case):
    fixture = receiver_case
    case = fixture.case
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at) VALUES('s_reference',?,?,'2026-09-14','2026-09-14')",
            (fixture.scope.workspace_id, fixture.scope.user_id),
        )
    common = {**case.common, "client_submission_id": str(uuid4()), "requested_session_id": "s_reference"}
    claim = await case.store.claim_chat_submission(**common)
    assert claim.kind == "claimed"
    case.args.update(
        client_submission_id=common["client_submission_id"],
        requested_session_id="s_reference",
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
    )
    fixture.scope = fixture.scope.model_copy(update={"session_id": "s_reference"})
    prepared = await candidate(fixture)
    await case.store.finalize_started_chat_submission(**case.args)
    assert prepared.skill_reads.scope.session_id == "s_reference"
    assert counts(case)["turn_agent_execution"] == 1


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_implicit_dependency_needing_parameters_is_not_admitted(receiver_case):
    prepared = await candidate(receiver_case)
    document = prepared.input_document.model_copy(
        update={
            "nodes": [node for node in prepared.input_document.nodes if not isinstance(node, ReferenceNode) or node.resource != receiver_case.child]
        }
    )
    with pytest.raises(SkillReadRenderError, match="^reference_skill_dependency_parameters_required$"):
        await prepare_skill_reference_start_admission(
            replace(prepared, input_document=document), sources=receiver_case.sources, scope=receiver_case.scope
        )
    _assert_no_acceptance(receiver_case.case)


def rebuild_closure(closure, *, items=None, roots=None):
    """Build coherent retained data for isolated structural rejection tests.

    Synthetic resources below exercise snapshot invariants only; they are not
    disk captures, approved package versions or current-source admission evidence.
    """
    import hashlib
    import json

    items = tuple(items) if items is not None else closure.items
    roots = tuple(roots) if roots is not None else closure.roots
    topology = []
    for item in items:
        topology.append(
            {
                "resource": item.proof.resource.model_dump(mode="json"),
                "required_skill_refs": item.material.document()["read_only_entry"]["required_skill_refs"],
                "content_hash": item.material.content_hash,
                "materialization_hash": item.material.materialization_hash,
                "source_state_digest": item.proof.source_state_digest,
            }
        )
    value = {
        "schema_version": 1,
        "scope": closure.scope.model_dump(mode="json"),
        "roots": [root.model_dump(mode="json") for root in roots],
        "items": topology,
    }
    digest = "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return replace(
        closure,
        items=items,
        roots=roots,
        closure_digest=digest,
        text_bytes=sum(len(file["text"].encode()) for item in items for file in item.material.document()["files"]),
        material_bytes=sum(len(item.material.content) for item in items),
    )


def synthetic_item(original, *, name, children=(), big=False):
    import hashlib
    import json

    resource = original.proof.resource.model_copy(update={"id": "builtin:" + name})
    document = original.material.document()
    document["resource"] = resource.model_dump(mode="json")
    document["read_only_entry"]["required_skill_refs"] = [child.model_dump(mode="json") for child in children]
    if big:
        body = "x" * (128 * 1024 + 1)
        file_hash = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        document["read_only_entry"]["required_files"] = ["reference.md"]
        document["files"] = [{"path": path, "text": body, "sha256": file_hash} for path in ("method.md", "reference.md")]

    def encode(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()

    content = encode(document)
    material = replace(
        original.material,
        content=content,
        content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
        materialization_hash="sha256:"
        + hashlib.sha256(encode({key: document[key] for key in ("schema_version", "read_only_entry", "files")})).hexdigest(),
    )
    return replace(original, proof=replace(original.proof, resource=resource, name=name), material=material)


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("change", ["depth", "package_bytes", "unreferenced", "root_count", "item_count", "proof_scope", "proof_epoch", "root_order"])
async def test_structurally_coherent_but_invalid_closure_is_rejected(receiver_case, change):
    prepared = await candidate(receiver_case)
    closure = prepared.skill_reads
    original = closure.items[0]
    if change == "depth":
        items = []
        for index in range(9):
            item = synthetic_item(original, name=f"chain-{index}", children=(items[-1].proof.resource,) if items else ())
            items.append(item)
        closure = rebuild_closure(closure, items=items, roots=[items[-1].proof.resource])
    elif change == "package_bytes":
        item = synthetic_item(original, name="large", big=True)
        closure = rebuild_closure(closure, items=[item], roots=[item.proof.resource])
    elif change == "unreferenced":
        closure = rebuild_closure(closure, items=[synthetic_item(original, name="unused"), *closure.items])
    elif change == "root_count":
        items = [synthetic_item(original, name=f"root-{index}") for index in range(4)]
        closure = rebuild_closure(closure, items=items, roots=[item.proof.resource for item in items])
    elif change == "item_count":
        leaves = [synthetic_item(original, name=f"item-{index}") for index in range(30)]
        branches = [
            synthetic_item(original, name=f"branch-{index}", children=tuple(item.proof.resource for item in leaves[index * 15 : (index + 1) * 15]))
            for index in range(2)
        ]
        root = synthetic_item(original, name="root", children=tuple(item.proof.resource for item in branches))
        closure = rebuild_closure(closure, items=[*leaves, *branches, root], roots=[root.proof.resource])
    elif change == "proof_scope":
        closure = rebuild_closure(
            closure,
            items=[replace(original, proof=replace(original.proof, scope=closure.scope.model_copy(update={"user_id": "other"}))), *closure.items[1:]],
        )
    elif change == "proof_epoch":
        closure = rebuild_closure(
            closure, items=[replace(original, proof=replace(original.proof, policy_epoch=original.proof.policy_epoch + 1)), *closure.items[1:]]
        )
    else:
        closure = rebuild_closure(closure, roots=tuple(reversed(closure.roots)))
    with pytest.raises((SkillReadClosureError, SkillReadRenderError)):
        if change == "root_order":
            replace(prepared, skill_reads=closure).snapshot()
        else:
            snapshot_prepared_skill_read_closure(closure)
    _assert_no_acceptance(receiver_case.case)


@pytest.mark.parametrize("receiver_case", ["admission_optional", "admission_optional_root_only"], indirect=True)
async def test_implicit_dependency_requires_its_own_baseline_without_becoming_a_user_node(receiver_case):
    prepared = await candidate(receiver_case, implicit=True)
    assert len(prepared.skill_reads.roots) == 1 and len(prepared.skill_reads.items) == 2
    grants = prepared.expected
    with sqlite3.connect(receiver_case.case.fixture.db_path) as db:
        import json

        definition = json.loads(db.execute("SELECT definition_json FROM team_subagent_revision WHERE id=?", (grants.revision_ref,)).fetchone()[0])
    if len(definition["skill_grants"]) == 1:
        with pytest.raises(SkillReadReceiverError, match="^reference_skill_extension_denied$"):
            await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
        _assert_no_acceptance(receiver_case.case)
    else:
        await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
        _, rendered = await prepare_reference_primary_skill_reads(
            receiver_case.sources, scope=receiver_case.scope.model_copy(update={"session_id": "s_reference"}), turn_id="t_reference"
        )
        items = rendered.document()["skills"]
        assert [(item["resource"]["id"], item["node_id"] is None, item["parameters"]) for item in items] == [
            ("builtin:support", True, {}),
            ("builtin:method", False, {}),
        ]
        with sqlite3.connect(receiver_case.case.fixture.db_path) as db:
            plan = json.loads(db.execute("SELECT plan_json FROM chat_reference_plans WHERE turn_id='t_reference'").fetchone()[0])
        assert len(plan["admissions"]) == 2


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("at_document_limit", [False, True])
async def test_system_candidate_is_detached_before_source_preparation_awaits(receiver_case, tmp_path, monkeypatch, at_document_limit):
    """Preflight fidelity only; this system fixture has no baseline Skill grants."""
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.chat_reference_admission import ReferenceStartAdmission
    from bowhead_core.chat_reference_scope import ReferenceRequestScope
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver
    from bowhead_core.space_governance import SpaceOperation
    from bowhead_gateway.access_context import build_access_context
    from test_agent_admission_transaction import _system_admission_case

    system = await _system_admission_case(tmp_path / "system")
    scope = ReferenceRequestScope(team_id=system.fixture.team_id, user_id=system.fixture.member_id, workspace_id=system.fixture.workspace_id)
    access = AccessControlStore(system.fixture.db_path)

    async def context():
        return await build_access_context(
            user_store=system.fixture.users,
            access_store=access,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            expected_team_id=scope.team_id,
            operation=SpaceOperation.EXECUTE,
        )

    sources = SkillReadSourceResolver(
        system.fixture.db_path, access_factory=context, builtin_index=receiver_case.sources.builtin_index, builtin_root=receiver_case.root
    )
    await sources.instances.initialize()
    document = InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                {"type": "text", "text": "Analyze using these methods"},
                {
                    "type": "reference",
                    "node_id": str(uuid4()),
                    "resource": {"kind": "agent", "source_kind": "system", "id": system.expected.agent_id, "version": system.expected.revision_ref},
                    "label": "Compute",
                    "parameters": {},
                },
                *[
                    {
                        "type": "reference",
                        "node_id": str(uuid4()),
                        "resource": resource.model_dump(mode="json"),
                        "label": "Method",
                        "parameters": {"year": 2026},
                    }
                    for resource in (receiver_case.selected, receiver_case.child)
                ],
            ],
        }
    )
    if at_document_limit:
        import json

        from bowhead_core.chat_references import MAX_DOCUMENT_BYTES

        value = document.model_dump(mode="python", exclude_none=True)
        size = len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        value["nodes"][0]["text"] += "x" * (MAX_DOCUMENT_BYTES - size)
        document = InputDocument.model_validate(value)
    admission = ReferenceStartAdmission(
        document,
        system.epoch,
        "compute",
        system.expected,
        system_revision=system.args["prepared_revision"],
        manifest_fingerprint=system.args["manifest_fingerprint"],
    )
    original_definition = dict(admission.system_revision.definition)
    original_prepare = sources.prepare

    async def interleave(*args, **kwargs):
        admission.system_revision.definition["llm_agent_type"] = "changed_during_preflight"
        return await original_prepare(*args, **kwargs)

    monkeypatch.setattr(sources, "prepare", interleave)
    prepared = await prepare_skill_reference_start_admission(admission, sources=sources, scope=scope)
    assert prepared.system_revision.definition == original_definition
    assert admission.system_revision.definition != original_definition

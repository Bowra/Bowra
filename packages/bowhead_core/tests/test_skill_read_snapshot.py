"""Retained closure integrity and actual scoped encrypted-content storage.

Storage tests supply an explicit internal worker fence; they do not exercise a
mixed scheduler, composition writer, model injection or current-use handler.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceCipher, ReferenceScope
from bowhead_core.chat_references import ReferenceNode
from bowhead_core.skill_read_material import SkillReadMaterialError, restore_skill_read
from bowhead_core.skill_read_primary import prepare_reference_primary_skill_reads
from bowhead_core.skill_read_render import render_prepared_skill_reads
from bowhead_core.skill_read_snapshot import SkillReadSnapshotError, prepare_skill_read_snapshot, restore_skill_read_snapshot
from bowhead_core.skill_upload import bundle_sha256
from cryptography.fernet import Fernet
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

receiver_case = _receiver_case
start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


@pytest.fixture(autouse=True)
def snapshot_body(request, monkeypatch):
    size = getattr(request, "param", None)
    if size is None:
        return
    import test_skill_read_receiver_integration as fixtures

    original = fixtures.package

    def package(root, name, *args, **kwargs):
        resource = original(root, name, *args, **kwargs)
        (root / name / "method.md").write_text("\x01" * size)
        return resource.model_copy(update={"version": "sha256:" + bundle_sha256(root / name)})

    # Change the files before the actual compiler, metadata index and source
    # preparation run. These hashes/grants refer to real installed test packages.
    monkeypatch.setattr(fixtures, "package", package)


@pytest.fixture
async def snapshot_case(receiver_case):
    prepared, rendered = await prepare_reference_primary_skill_reads(receiver_case.sources, scope=receiver_case.scope, turn_id="t_reference")
    # This fixture exercises the supported v1 method-only snapshot. Whole
    # packages belong to the distinct v2 snapshot/claim compatibility tests.
    prepared = replace(prepared, baseline=None)
    snapshot = prepare_skill_read_snapshot(prepared)
    descriptor = snapshot.descriptor(node_id="00000000-0000-4000-8000-000000000001", content_key="initial-skill-closure")
    with sqlite3.connect(receiver_case.case.fixture.db_path) as db:
        from bowhead_core.chat_references import InputDocument

        document = InputDocument.model_validate_json(
            db.execute("SELECT input_document_json FROM chat_turn_user_sources WHERE turn_id='t_reference'").fetchone()[0]
        )
    selections = [node for node in document.nodes if isinstance(node, ReferenceNode) and node.resource.kind == "skill"]
    return SimpleNamespace(
        fixture=receiver_case,
        prepared=prepared,
        rendered=rendered,
        snapshot=snapshot,
        descriptor=descriptor,
        selections=selections,
        cipher=ReferenceCipher(Fernet.generate_key().decode()),
    )


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
async def test_closure_round_trip_retains_exact_members_and_renders_original_parameters(snapshot_case, monkeypatch):
    case = snapshot_case
    raw = json.loads(case.snapshot.content)
    assert [member["proof"]["resource"]["id"] for member in raw["closure"]["items"]] == ["builtin:support", "builtin:method"]
    assert case.descriptor.resource == case.prepared.closure.roots[0]
    assert case.descriptor.resource != case.prepared.closure.items[0].proof.resource
    for member, original in zip(raw["closure"]["items"], case.prepared.closure.items, strict=True):
        assert base64.b64decode(member["content"]) == original.material.content
        assert member["content_hash"] == original.material.content_hash
        assert member["materialization_hash"] == original.material.materialization_hash
    for method in ("method", "support"):
        (case.fixture.root / method / "method.md").unlink()

    def forbidden(*args, **kwargs):
        raise AssertionError("restore must use retained material")

    with monkeypatch.context() as patch:
        for name in ("resolve", "stat", "open", "read_bytes", "read_text"):
            patch.setattr(Path, name, forbidden)
        restored = restore_skill_read_snapshot(case.snapshot.content, descriptor=case.descriptor, receiver=case.prepared.receiver)
        assert restored == case.prepared
        assert render_prepared_skill_reads(restored, case.selections) == case.rendered
    assert "BODY_" not in repr(case.snapshot)
    with pytest.raises(SkillReadMaterialError):
        restore_skill_read(case.snapshot.content, descriptor=case.descriptor)


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
@pytest.mark.parametrize("change", ["execution", "source", "scope", "status", "policy"])
async def test_restore_requires_the_independent_original_receiver(snapshot_case, change):
    case = snapshot_case
    receiver = case.prepared.receiver
    update = {
        "execution": {"execution_id": "another-execution"},
        "source": {"source_hash": "sha256:" + "a" * 64},
        "scope": {"scope": receiver.scope.model_copy(update={"user_id": "another-user"})},
        "status": {"status": "running"},
        "policy": {"policy_fingerprint": "sha256:" + "b" * 64},
    }[change]
    with pytest.raises(SkillReadSnapshotError, match="^reference_skill_snapshot_invalid$"):
        restore_skill_read_snapshot(case.snapshot.content, descriptor=case.descriptor, receiver=receiver.model_copy(update=update))


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
@pytest.mark.parametrize("change", ["content_hash", "materialization_hash", "byte_count", "anchor", "kind"])
async def test_restore_checks_the_stored_descriptor(snapshot_case, change):
    case = snapshot_case
    update = {
        "content_hash": {"content_hash": "sha256:" + "0" * 64},
        "materialization_hash": {"materialization_hash": "sha256:" + "0" * 64},
        "byte_count": {"byte_count": case.descriptor.byte_count + 1},
        "anchor": {"resource": case.prepared.closure.roots[1]},
        "kind": {"content_kind": "skill_read"},
    }[change]
    with pytest.raises(SkillReadSnapshotError):
        restore_skill_read_snapshot(case.snapshot.content, descriptor=case.descriptor.model_copy(update=update), receiver=case.prepared.receiver)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
@pytest.mark.parametrize(
    "change",
    ["version", "contract", "receiver", "preparation", "unknown", "missing_child", "order", "material", "base64", "proof", "counter", "roots"],
)
async def test_internally_changed_payload_cannot_use_its_own_hash_as_proof(snapshot_case, change):
    case = snapshot_case
    value = json.loads(case.snapshot.content)
    closure = value["closure"]
    if change == "version":
        value["schema_version"] = True
    elif change == "contract":
        value["snapshot_contract"] = "future-contract"
    elif change == "receiver":
        value["receiver_digest"] = "sha256:" + "a" * 64
    elif change == "preparation":
        value["preparation_digest"] = "sha256:" + "a" * 64
    elif change == "unknown":
        value["unexpected"] = "PRIVATE_METADATA"
    elif change == "missing_child":
        closure["items"] = closure["items"][1:]
    elif change == "order":
        closure["items"].reverse()
    elif change == "material":
        closure["items"][0]["content"] = base64.b64encode(b"PRIVATE_CHANGED_BODY").decode()
    elif change == "base64":
        closure["items"][0]["content"] += "\n"
    elif change == "proof":
        closure["items"][0]["proof"]["policy_epoch"] = False
    elif change == "counter":
        closure["material_bytes"] += 1
    else:
        closure["roots"].reverse()
    content = encode(value)
    # Supply a new hash to reach internal structure checks. This is not a claim
    # that a caller can rewrite the actual immutable descriptor or ciphertext.
    descriptor = case.descriptor.model_copy(update={"content_hash": "sha256:" + hashlib.sha256(content).hexdigest(), "byte_count": len(content)})
    with pytest.raises(SkillReadSnapshotError, match="^reference_skill_snapshot_invalid$"):
        restore_skill_read_snapshot(content, descriptor=descriptor, receiver=case.prepared.receiver)


async def seed_worker(case):
    """Explicit storage fence fixture, not a claimed production mixed worker."""
    scope = ReferenceScope(**case.prepared.receiver.scope.model_dump(), policy_epoch=case.prepared.closure.items[0].proof.policy_epoch)
    now = datetime.now(UTC).isoformat()
    lease = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    with sqlite3.connect(case.fixture.case.fixture.db_path) as db:
        db.execute("UPDATE turn_agent_execution SET status='running',started_at=? WHERE turn_id='t_reference'", (now,))
        db.execute(
            "INSERT INTO chat_turn_spawn_dispatch(turn_id,session_id,dispatch_kind,state,owner_id,lease_expires_at,created_at,updated_at) "
            "VALUES('t_reference','s_reference','start','spawned','snapshot-worker',?,?,?)",
            (lease, now, now),
        )
    return scope, (datetime.now(UTC) + timedelta(days=1)).isoformat()


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
async def test_real_store_encrypts_one_closure_without_dependency_plan_nodes(snapshot_case):
    case = snapshot_case
    scope, expires = await seed_worker(case)
    store = ChatReferenceStore(case.cipher)
    args = {
        "scope": scope,
        "turn_id": "t_reference",
        "descriptor": case.descriptor,
        "content": case.snapshot.content,
        "generation": 1,
        "owner_id": "snapshot-worker",
        "expires_at": expires,
    }
    async with open_shared_ctx(case.fixture.case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await store.put_content_in_connection(db, **args)
        assert not await store.put_content_in_connection(db, **args)
        await db.commit()
    async with open_shared_ctx(case.fixture.case.fixture.db_path) as db:
        await db.execute("BEGIN")
        descriptor, ciphertext = await store.get_encrypted_content_in_connection(
            db, scope=scope, turn_id="t_reference", content_key=case.descriptor.content_key
        )
        await db.rollback()

    assert case.snapshot.content.decode() not in ciphertext
    content = case.cipher.open(scope=scope, purpose="content", identity=f"t_reference/{descriptor.content_key}", ciphertext=ciphertext)
    assert restore_skill_read_snapshot(content, descriptor=descriptor, receiver=case.prepared.receiver) == case.prepared
    with sqlite3.connect(case.fixture.case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM chat_reference_content_snapshots").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions").fetchone() == (0,)
        assert db.execute("SELECT json_array_length(plan_json,'$.admissions') FROM chat_reference_plans").fetchone() == (3,)
        db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'")
    async with open_shared_ctx(case.fixture.case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="reference_source_unavailable"):
            await store.get_encrypted_content_in_connection(db, scope=scope, turn_id="t_reference", content_key=case.descriptor.content_key)
        with pytest.raises(ValueError, match="reference_source_unavailable"):
            await store.put_content_in_connection(db, **args)
        await db.rollback()


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
@pytest.mark.parametrize("snapshot_body", [64000, 65536], indirect=True)
async def test_final_snapshot_budget_counts_base64_and_metadata(receiver_case, request):
    prepared, _ = await prepare_reference_primary_skill_reads(receiver_case.sources, scope=receiver_case.scope, turn_id="t_reference")
    assert prepared.closure.material_bytes < 1024 * 1024
    if request.node.callspec.params["snapshot_body"] == 65536:
        with pytest.raises(SkillReadSnapshotError, match="^reference_skill_snapshot_material_limit$"):
            prepare_skill_read_snapshot(prepared)
    else:
        snapshot = prepare_skill_read_snapshot(prepared)
        assert prepared.closure.material_bytes < len(snapshot.content) <= 1024 * 1024


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
async def test_source_proof_name_must_match_the_original_exact_baseline(snapshot_case):
    prepared = snapshot_case.prepared
    item = prepared.closure.items[0]
    changed = replace(item, proof=replace(item.proof, name="another-method"))
    closure = replace(prepared.closure, items=(changed, *prepared.closure.items[1:]))
    with pytest.raises(SkillReadSnapshotError):
        prepare_skill_read_snapshot(replace(prepared, closure=closure))


@pytest.mark.parametrize("receiver_case", ["admission_optional"], indirect=True)
async def test_implicit_dependency_retains_its_own_bytes_and_requires_its_original_grant(receiver_case):
    from test_skill_start_admission import candidate

    await candidate(receiver_case, implicit=True)
    await receiver_case.case.store.finalize_started_chat_submission(**receiver_case.case.args)
    scope = receiver_case.scope.model_copy(update={"session_id": "s_reference"})
    prepared, _ = await prepare_reference_primary_skill_reads(receiver_case.sources, scope=scope, turn_id="t_reference")
    snapshot = prepare_skill_read_snapshot(prepared)
    assert len(prepared.closure.roots) == 1 and len(json.loads(snapshot.content)["closure"]["items"]) == 2
    receiver = prepared.receiver.model_copy(
        update={"grants": tuple(grant for grant in prepared.receiver.grants if grant.id != receiver_case.child.id)}
    )
    with pytest.raises(SkillReadSnapshotError):
        prepare_skill_read_snapshot(replace(prepared, receiver=receiver))


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
async def test_receiver_evidence_and_material_identity_have_separate_hashes(snapshot_case):
    case = snapshot_case
    receiver = case.prepared.receiver.model_copy(update={"policy_fingerprint": "sha256:" + "c" * 64})
    other = prepare_skill_read_snapshot(replace(case.prepared, receiver=receiver))
    assert other.content_hash != case.snapshot.content_hash
    assert other.materialization_hash == case.snapshot.materialization_hash


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
async def test_preflight_scope_without_a_real_session_cannot_become_a_material_snapshot(snapshot_case):
    from test_skill_start_admission import rebuild_closure

    prepared = snapshot_case.prepared
    scope = prepared.closure.scope.model_copy(update={"session_id": None})
    closure = rebuild_closure(
        replace(prepared.closure, scope=scope),
        items=[replace(item, proof=replace(item.proof, scope=scope)) for item in prepared.closure.items],
    )
    with pytest.raises(SkillReadSnapshotError):
        prepare_skill_read_snapshot(replace(prepared, receiver=prepared.receiver.model_copy(update={"scope": scope}), closure=closure))


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
@pytest.mark.parametrize("stage", ["prepare", "descriptor", "restore"])
async def test_snapshot_errors_keep_private_values_out_of_warnings_and_exception_chains(snapshot_case, stage, recwarn):
    case = snapshot_case
    secret = "PRIVATE_SNAPSHOT_METADATA_SENTINEL"
    with pytest.raises(SkillReadSnapshotError) as failure:
        if stage == "prepare":
            receiver = case.prepared.receiver.model_copy(update={"execution_id": secret.encode()})
            prepare_skill_read_snapshot(replace(case.prepared, receiver=receiver))
        elif stage == "descriptor":
            case.snapshot.descriptor(node_id=secret, content_key="invalid-node")
        else:
            content = secret.encode()
            descriptor = case.descriptor.model_copy(
                update={"content_hash": "sha256:" + hashlib.sha256(content).hexdigest(), "byte_count": len(content)}
            )
            restore_skill_read_snapshot(content, descriptor=descriptor, receiver=case.prepared.receiver)
    assert secret not in "".join(traceback.format_exception(failure.value))
    assert not recwarn.list


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
@pytest.mark.parametrize("snapshot_body", [None, 4096], indirect=True)
async def test_legacy_snapshot_evidence_extracts_one_composed_member(snapshot_case):
    from bowhead_core.chat_reference_projection import _skill_member_projection
    from bowhead_core.skill_reference_execution import SkillCompositionMember

    case = snapshot_case
    scope = ReferenceScope(**case.prepared.receiver.scope.model_dump(), policy_epoch=case.prepared.closure.items[0].proof.policy_epoch)
    selected = case.prepared.closure.items[-1]
    member = SkillCompositionMember(
        resource=selected.proof.resource, content_hash=selected.material.content_hash, materialization_hash=selected.material.materialization_hash
    )
    ciphertext = case.cipher.seal(
        scope=scope, purpose="content", identity=f"t_reference/{case.descriptor.content_key}", content=case.snapshot.content
    )
    args = {
        "member": member,
        "descriptor": case.descriptor,
        "scope": scope,
        "turn_id": "t_reference",
        "include_content": True,
        "cipher": case.cipher,
        "material_available": True,
        "row": {"retention_state": "available", "ciphertext": ciphertext},
    }
    result = _skill_member_projection(**args)
    assert result["content_state"] == "included"
    assert result["content_excerpt"] == selected.material.content.decode()[:4096]
    assert result["content_truncated"] is (len(selected.material.content.decode()) > 4096)
    assert result["byte_count"] == len(selected.material.content)
    assert "BODY_support" not in result["content_excerpt"]
    assert _skill_member_projection(**{**args, "include_content": False})["content_state"] == "not_requested"
    assert _skill_member_projection(**{**args, "cipher": None})["content_state"] == "key_unavailable"
    for patch in (
        {"turn_id": "another_turn"},
        {"descriptor": case.descriptor.model_copy(update={"content_hash": "sha256:" + "a" * 64})},
        {"member": member.model_copy(update={"content_hash": "sha256:" + "b" * 64})},
        {"member": member.model_copy(update={"materialization_hash": "sha256:" + "c" * 64})},
    ):
        missing = _skill_member_projection(**{**args, **patch})
        assert missing["content_state"] == "unavailable"
        assert "content_excerpt" not in missing

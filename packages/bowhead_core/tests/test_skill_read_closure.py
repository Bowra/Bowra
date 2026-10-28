"""Graph rules at the source-store boundary; real IO integration lives in Gateway.

Synthetic source results make cycles and inconsistent versions representable
without claiming that cyclic whole-package hashes can be authored on disk.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.skill_read_closure import SkillReadClosureError, prepare_skill_read_closure
from bowhead_core.skill_read_material import PreparedSkillRead
from bowhead_core.skill_read_resolution import ResolvedSkillRead, SkillReadResolutionError, SkillReadSourceProof

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
SCOPE = ReferenceRequestScope(team_id="team", user_id="requester", workspace_id="workspace", session_id="session")


def ref(name, *, source="user", version=None):
    return SkillResourceRef(kind="skill", source_kind=source, id=name, version=version or "sha256:" + hashlib.sha256(name.encode()).hexdigest())


def material(resource, children=(), *, texts=("METHOD",), epoch=0):
    content = json.dumps(
        {
            "schema_version": 1,
            "resource": resource.model_dump(mode="json"),
            "read_only_entry": {"required_skill_refs": [child.model_dump(mode="json") for child in children]},
            "files": [{"path": f"method{index}.md", "text": text} for index, text in enumerate(texts)],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    proof = SkillReadSourceProof(
        SCOPE,
        resource,
        resource.id,
        epoch,
        "personal_owner_current_bytes",
        "sha256:" + hashlib.sha256((resource.id + str(epoch)).encode()).hexdigest(),
    )
    return ResolvedSkillRead(proof, PreparedSkillRead(content, digest, digest))


class Sources:
    def __init__(self, records, *, scope=SCOPE):
        self.scope = scope
        self.records = {(item.proof.resource.source_kind, item.proof.resource.id): item for item in records}
        self.reads = []
        self.checked = []
        self.denied = set()
        self.after_read = lambda resource: None

    async def prepare(self, resource, *, scope):
        assert scope == self.scope
        self.reads.append(resource.id)
        if resource.id in self.denied:
            raise SkillReadResolutionError("reference_skill_source_unavailable")
        result = self.records[(resource.source_kind, resource.id)]
        self.after_read(resource)
        return result

    async def revalidate(self, proof):
        self.checked.append(proof.resource.id)
        if proof.resource.id in self.denied:
            raise SkillReadResolutionError("reference_skill_source_unavailable")


async def test_diamond_is_prepared_once_dependency_first_and_scope_local():
    a, b, c = map(ref, ("a", "b", "shared"))
    sources = Sources([material(a, (c,)), material(b, (c,)), material(c)])
    result = await prepare_skill_read_closure(sources, [a, b], scope=SCOPE)
    assert result.roots == (a, b) and result.scope == SCOPE
    assert [item.proof.resource for item in result.items] == [c, a, b]
    assert sources.reads == ["a", "shared", "b"]
    assert sources.checked == ["shared", "a", "b"]
    assert result.text_bytes == len("METHOD") * 3
    assert result.material_bytes == sum(len(item.material.content) for item in sources.records.values())
    assert "METHOD" not in repr(result) and "personal_owner_current_bytes" not in repr(result)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.items = ()
    again = await prepare_skill_read_closure(sources, [a, b], scope=SCOPE)
    assert again.closure_digest == result.closure_digest
    assert sources.reads == ["a", "shared", "b"] * 2
    reversed_roots = await prepare_skill_read_closure(sources, [b, a], scope=SCOPE)
    assert reversed_roots.closure_digest != result.closure_digest
    sources.records = {
        key: dataclasses.replace(item, proof=dataclasses.replace(item.proof, policy_epoch=1, source_state_digest="sha256:" + "1" * 64))
        for key, item in sources.records.items()
    }
    new_policy = await prepare_skill_read_closure(sources, [a, b], scope=SCOPE)
    assert new_policy.closure_digest != result.closure_digest


@pytest.mark.parametrize("selection", [[], [ref(str(i)) for i in range(4)], [ref("a"), ref("a")], [ref("a"), ref("a", version="sha256:" + "0" * 64)]])
async def test_invalid_root_selection_fails_before_source_io(selection):
    sources = Sources([material(resource) for resource in selection])
    with pytest.raises(SkillReadClosureError):
        await prepare_skill_read_closure(sources, selection, scope=SCOPE)
    assert not sources.reads


async def test_transitive_versions_do_not_pick_the_first_or_last():
    a, b = ref("a"), ref("b")
    different = ref("b", version="sha256:" + "0" * 64)
    sources = Sources([material(a, (different,)), material(b)])
    with pytest.raises(SkillReadClosureError, match="^reference_skill_dependency_version_conflict$"):
        await prepare_skill_read_closure(sources, [a, b], scope=SCOPE)
    assert sources.reads == ["a"]


async def test_cycle_is_rejected_before_reopening_the_ancestor():
    a, b = ref("a"), ref("b")
    sources = Sources([material(a, (b,)), material(b, (a,))])
    with pytest.raises(SkillReadClosureError, match="^reference_skill_dependency_cycle$"):
        await prepare_skill_read_closure(sources, [a], scope=SCOPE)
    assert sources.reads == ["a", "b"]


@pytest.mark.parametrize("length", [8, 9])
async def test_dependency_depth_counts_root_as_one(length):
    refs = [ref(str(i)) for i in range(length)]
    sources = Sources([material(resource, refs[index + 1 : index + 2]) for index, resource in enumerate(refs)])
    if length == 8:
        assert len((await prepare_skill_read_closure(sources, refs[:1], scope=SCOPE)).items) == 8
    else:
        with pytest.raises(SkillReadClosureError, match="^reference_skill_dependency_limit$"):
            await prepare_skill_read_closure(sources, refs[:1], scope=SCOPE)
        assert len(sources.reads) == 8


async def test_shared_subtree_cannot_bypass_depth_when_later_reached_by_longer_path():
    chain = [ref(str(i)) for i in range(7)]
    a, b, link = ref("a"), ref("b"), ref("link")
    sources = Sources(
        [material(resource, chain[index + 1 : index + 2]) for index, resource in enumerate(chain)]
        + [material(a, chain[:1]), material(b, (link,)), material(link, chain[:1])]
    )
    with pytest.raises(SkillReadClosureError, match="^reference_skill_dependency_limit$"):
        await prepare_skill_read_closure(sources, [a, b], scope=SCOPE)
    assert sources.reads.count(chain[0].id) == 1


@pytest.mark.parametrize("count", [32, 33])
async def test_total_nodes_include_shared_dependencies_and_roots(count):
    root, a, b = map(ref, ("root", "a", "b"))
    leaves = [ref(str(i)) for i in range(count - 3)]
    sources = Sources([material(root, (a, b)), material(a, leaves[:15]), material(b, leaves[15:])] + [material(leaf) for leaf in leaves])
    if count == 32:
        assert len((await prepare_skill_read_closure(sources, [root], scope=SCOPE)).items) == count
    else:
        with pytest.raises(SkillReadClosureError, match="^reference_skill_dependency_limit$"):
            await prepare_skill_read_closure(sources, [root], scope=SCOPE)
        assert len(sources.reads) == 32


@pytest.mark.parametrize("extra", [0, 1])
async def test_package_budget_sums_all_declared_files_in_utf8(extra):
    resource = ref("method")
    sources = Sources([material(resource, texts=("é" * 65536, "x" * (131072 + extra)))])
    if not extra:
        assert (await prepare_skill_read_closure(sources, [resource], scope=SCOPE)).text_bytes == 256 * 1024
    else:
        with pytest.raises(SkillReadClosureError, match="^reference_skill_dependency_material_limit$"):
            await prepare_skill_read_closure(sources, [resource], scope=SCOPE)


@pytest.mark.parametrize("text", ["x" * 250000, "\n" * 150000])
async def test_total_material_budget_includes_json_escaping_and_dependencies(text):
    refs = [ref(str(i)) for i in range(5 if text[0] == "x" else 4)]
    sources = Sources([material(resource, refs[index + 1 : index + 2], texts=(text,)) for index, resource in enumerate(refs)])
    with pytest.raises(SkillReadClosureError, match="^reference_skill_dependency_material_limit$"):
        await prepare_skill_read_closure(sources, refs[:1], scope=SCOPE)


async def test_dependency_use_and_post_graph_revocation_are_both_required():
    a, b = ref("a"), ref("b")
    sources = Sources([material(a, (b,)), material(b)])
    sources.denied.add(b.id)
    with pytest.raises(SkillReadResolutionError):
        await prepare_skill_read_closure(sources, [a], scope=SCOPE)
    assert not sources.checked
    sources.denied.clear()
    sources.after_read = lambda resource: sources.denied.add(a.id) if resource == b else None
    with pytest.raises(SkillReadResolutionError):
        await prepare_skill_read_closure(sources, [a], scope=SCOPE)
    assert sources.checked == ["b", "a"]


async def test_policy_epoch_cannot_change_between_nodes():
    a, b = ref("a"), ref("b")
    sources = Sources([material(a, (b,), epoch=1), material(b, epoch=2)])
    with pytest.raises(SkillReadClosureError, match="^reference_skill_source_changed$"):
        await prepare_skill_read_closure(sources, [a], scope=SCOPE)


async def test_equal_names_in_different_origins_are_distinct():
    user, team = ref("method"), ref("method", source="team")
    sources = Sources([material(user), material(team)])
    assert len((await prepare_skill_read_closure(sources, [user, team], scope=SCOPE)).items) == 2


async def test_caller_mutation_during_source_io_cannot_replace_selected_roots():
    a, b, other = map(ref, ("a", "b", "other"))
    selection = [a, b]
    sources = Sources([material(a), material(b)])
    sources.after_read = lambda resource: selection.__setitem__(1, other)
    result = await prepare_skill_read_closure(sources, selection, scope=SCOPE)
    assert result.roots == (a, b) and sources.reads == ["a", "b"]


async def test_source_adapter_scope_mismatch_and_failure_are_finite():
    resource = ref("method")
    wrong = material(resource)
    wrong = dataclasses.replace(wrong, proof=dataclasses.replace(wrong.proof, scope=SCOPE.model_copy(update={"user_id": "other"})))
    with pytest.raises(SkillReadClosureError, match="^reference_skill_dependency_unavailable$"):
        await prepare_skill_read_closure(Sources([wrong]), [resource], scope=SCOPE)
    with pytest.raises(SkillReadClosureError, match="^reference_skill_dependency_unavailable$"):
        await prepare_skill_read_closure(Sources([]), [ref("PRIVATE_RESOURCE")], scope=SCOPE)


async def test_preparation_digest_includes_session_even_when_source_state_is_unchanged():
    resource = ref("method")
    original = material(resource)
    first = await prepare_skill_read_closure(Sources([original]), [resource], scope=SCOPE)
    other_scope = SCOPE.model_copy(update={"session_id": "other_session"})
    other = dataclasses.replace(original, proof=dataclasses.replace(original.proof, scope=other_scope))
    second = await prepare_skill_read_closure(Sources([other], scope=other_scope), [resource], scope=other_scope)
    assert first.items[0].material.content_hash == second.items[0].material.content_hash
    assert first.closure_digest != second.closure_digest

"""Retained inputs cannot silently exceed the original bounded material scope."""

from types import SimpleNamespace

import pytest
from bowhead_core.chat_reference_attachment_snapshot import MAX_LEAD_ATTACHMENT_TOTAL_BYTES
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
from bowhead_core.ordinary_lead_input_context import retained_ids, validate_shape


@pytest.mark.parametrize("boundary", ["unscoped", "missing_current", "duplicate_parsed", "duplicate_pin", "count", "size", "negative", "boolean"])
def test_retained_context_refuses_incomplete_or_unbounded_projection(boundary):
    source = SimpleNamespace(attachment_ids=("current",))
    context = {"parsed": [{"artifact_id": "current", "size": 1}, {"artifact_id": "prior", "size": 1}], "pinned": [{"artifact_id": "prior"}]}
    if boundary == "unscoped":
        context["pinned"] = []
    elif boundary == "missing_current":
        context["parsed"].pop(0)
    elif boundary == "duplicate_parsed":
        context["parsed"].append(context["parsed"][0])
    elif boundary == "duplicate_pin":
        context["pinned"].append(context["pinned"][0])
    elif boundary == "count":
        context["pinned"].extend({"artifact_id": f"art_{i}"} for i in range(31))
    else:
        context["parsed"][1]["size"] = {"size": MAX_LEAD_ATTACHMENT_TOTAL_BYTES, "negative": -1, "boolean": True}[boundary]
    with pytest.raises(OrdinaryContinuationUnavailable):
        retained_ids(source, context)


@pytest.mark.parametrize("bad_provenance", [[], {}, None, "prompt_claim"])
def test_retained_context_refuses_malformed_provenance(bad_provenance):
    proof = {"artifacts": [{"artifact_id": "art_prior", "provenance": bad_provenance, "metadata_hash": "a" * 64}], "attachments": None}
    with pytest.raises(OrdinaryContinuationUnavailable):
        validate_shape(proof, {"parsed": [], "pinned": [{"artifact_id": "art_prior"}]})


def test_reference_inventory_preserves_aliases_and_rejects_ambiguous_identity():
    from bowhead_core._ref_allowlist import RefAllowlist

    original = RefAllowlist()
    original.add("data", "art_original", filename="original.txt")
    original.add("code", "sec_code")
    encoded = original.snapshot()
    restored = RefAllowlist.from_snapshot(encoded)
    assert restored.resolve("1") == "art_original" and restored.resolve("2") == "sec_code"
    assert restored.to_prompt_xml() == original.to_prompt_xml()
    original.add("data", "art_later", filename="later.txt")
    assert restored.snapshot() == encoded
    assert restored.add("data", "art_result", filename="result.txt") == 3
    with pytest.raises(ValueError, match="reference_inventory_unavailable"):
        RefAllowlist.from_snapshot([*encoded, encoded[0]])
    with pytest.raises(ValueError, match="reference_inventory_unavailable"):
        RefAllowlist.from_snapshot([{"kind": "data", "id": "art_original", "meta": {"real_id": "art_other"}}])


@pytest.mark.asyncio
async def test_resumed_reference_inventory_keeps_aliases_and_allows_new_same_turn_outputs():
    from bowhead_core._ref_allowlist import RefAllowlist

    original = RefAllowlist()
    original.add("data", "art_original", filename="original.txt")
    retained = RefAllowlist.from_snapshot(original.snapshot())

    async def list_for_session(**_kwargs):
        return [
            SimpleNamespace(id="art_later", filename="later.txt", created_by_turn=None),
            SimpleNamespace(id="art_other_turn", filename="other.txt", created_by_turn="t_other"),
            SimpleNamespace(id="art_result", filename="result.txt", created_by_turn="t_original"),
            SimpleNamespace(id="art_original", filename="original.txt", created_by_turn=None),
        ]

    await retained.refresh_from_stores(
        canvas_store=None,
        artifact_store=SimpleNamespace(list_for_session=list_for_session),
        session_id="s_original",
        user_id="usr_original",
        canvas_id=None,
        tool_invocations=[],
        retained_artifact_ids=frozenset({"art_original"}),
        current_turn_id="t_original",
    )
    assert retained.real_ids_of_kinds(("data",)) == {"art_original", "art_result"}
    assert retained.resolve("1") == "art_original" and retained.resolve("2") == "art_result"
    assert "later.txt" not in retained.to_prompt_xml() and "other.txt" not in retained.to_prompt_xml()

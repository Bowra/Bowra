"""Retained artifact identity preserves old proofs and security metadata."""

from dataclasses import asdict, fields, replace

import pytest
from bowhead_core.artifact_store import Artifact
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, content_hash
from bowhead_core.ordinary_lead_material import _artifact_metadata_hash

pytestmark = pytest.mark.unit
ARTIFACT = Artifact(
    "art_owned", "result.txt", "text/plain", 3, "python_sandbox", "result.txt", None, "u_owner", "2026-10-06", workspace_id="ws_owned"
)


@pytest.mark.parametrize(
    "contract", ["ordinary-lead-loop.v1", "ordinary-lead-loop.v2", "ordinary-lead-loop.v3", "ordinary-lead-loop.v4", "ordinary-lead-loop.v5"]
)
@pytest.mark.parametrize("field", [field.name for field in fields(Artifact)])
def test_only_declared_non_identity_fields_can_change(contract, field):
    changed = replace(ARTIFACT, **{field: 4 if field == "size" else "changed"})
    ignored = {"last_used_at"}
    if contract in {"ordinary-lead-loop.v3", "ordinary-lead-loop.v4", "ordinary-lead-loop.v5"}:
        ignored.update({"category", "category_source", "retention_hint"})
    assert (_artifact_metadata_hash(changed, contract=contract) == _artifact_metadata_hash(ARTIFACT, contract=contract)) is (field in ignored)
    if contract in {"ordinary-lead-loop.v1", "ordinary-lead-loop.v2"}:
        original = asdict(ARTIFACT)
        original.pop("last_used_at")
        assert _artifact_metadata_hash(ARTIFACT, contract=contract) == content_hash(original)


def test_unknown_identity_contract_cannot_choose_a_digest():
    with pytest.raises(OrdinaryContinuationUnavailable):
        _artifact_metadata_hash(ARTIFACT, contract="ordinary-lead-loop.unknown")

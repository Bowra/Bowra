"""The installed DuPont method participates in exact read-only composition."""

import json
import runpy
from pathlib import Path

import pytest
from bowhead_core.agent_dependency_compiler import ExactSkillGrant
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.reference_receiver_metadata import ReferenceReceiverMetadata
from bowhead_core.reference_skill_compatibility import ReferenceSkillMetadata, reference_skill_modes
from bowhead_core.skill_read_material import prepare_skill_read, validate_prepared_skill_read
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from bowhead_skill.loader import load_skill
from test_skill_read_extensions import policy_for

from bowhead_skill.catalog import builtin_catalog_dir

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("permissions", [("read",), ("invoke", "read")])
def test_installed_dupont_projects_independent_method_without_changing_grants(permissions):
    root = builtin_catalog_dir()
    item = next(item for item in build_builtin_skill_metadata().items if item.name == "analyst-method-roe-dupont")
    resource = SkillResourceRef(kind="skill", source_kind="builtin", id=item.id, version=item.revision)
    grant = ExactSkillGrant("builtin", item.id, item.name, permissions, item.revision, "reference")
    receiver = ReferenceReceiverMetadata((grant,))
    metadata = {resource: ReferenceSkillMetadata(item.name, item.read_only_entry_json, True, True)}

    assert reference_skill_modes(receiver, [resource], metadata) == ("baseline",)
    assert reference_skill_modes(ReferenceReceiverMetadata(()), [resource], metadata) is None
    assert reference_skill_modes(ReferenceReceiverMetadata((), policy_for(resource)), [resource], metadata) == ("readonly_extension",)
    assert receiver.grants == (grant,)

    path = Path(root) / item.name
    prepared = prepare_skill_read(path, resource=resource, expected_name=item.name)
    assert validate_prepared_skill_read(prepared, resource=resource) == prepared
    document = prepared.document()
    assert [file["path"] for file in document["files"]] == ["references/readonly-method.md"]
    assert document["files"][0]["text"] == (path / "references/readonly-method.md").read_text()
    assert document["read_only_entry"]["required_skill_refs"] == []
    entry = load_skill(path).read_only_entry
    assert entry is not None
    assert entry.validate_parameters({}) == {}
    assert entry.validate_parameters({"company": "Example", "period": "2026 Q3"}) == {"company": "Example", "period": "2026 Q3"}


@pytest.mark.parametrize("case_id", ["case_1_strong_organic_roe", "case_2_roic_below_wacc", "case_3_low_tax_burden"])
def test_dupont_numerical_eval_matches_the_existing_calculation(case_id):
    path = Path(builtin_catalog_dir()) / "analyst-method-roe-dupont"
    case = next(case for case in json.loads((path / "evals/evals.json").read_text())["cases"] if case["id"] == case_id)
    calculate = runpy.run_path(str(path / "scripts/roe_dupont.py"))["run_roe_dupont"]
    result = calculate(case["inputs"])
    for name, expected in case["expected_metrics"].items():
        assert result["metrics"][name] == pytest.approx(expected), f"Incorrect expected {name} in {case_id}"

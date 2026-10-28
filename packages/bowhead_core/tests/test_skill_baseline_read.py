"""Original SOP/reference reads are distinct from selected method parameters."""

from __future__ import annotations

import shutil

import pytest
from bowhead_core.skill_baseline_read import read_baseline_package, snapshot_skill_read_arguments
from bowhead_core.skill_package_material import prepare_skill_package
from test_skill_read_material import _ref, _skill

pytestmark = pytest.mark.unit


@pytest.fixture
def package(tmp_path):
    root = _skill(tmp_path / "sop")
    (root / "SKILL.md").write_text(
        "---\nname: dupont\ndescription: Profitability\nallowed-tools: bi_query\npipes_output_to: summary\nversion: '1.2'\n---\n"
        "Original SOP $ARGUMENTS / $1"
    )
    (root / "references/binary.bin").write_bytes(b"\x00\xff")
    material = prepare_skill_package(root, resource=_ref(root), expected_name="dupont")
    shutil.rmtree(root)
    return material


@pytest.mark.parametrize("arguments", ["2026 firm", ["2026", "firm"]])
def test_original_sop_and_argument_contract_work_without_source_files(package, arguments):
    result = read_baseline_package(package, {"name": "dupont", "args": arguments})
    assert "2026 firm" in result["body"] and "$ARGUMENTS" not in result["body"]
    assert result["version"] == "1.2" and result["allowed_tools"] == ["bi_query"]
    assert result["resource"] == package.resource.model_dump(mode="json")
    assert result["script_execution"] == "disabled" and result["scripts"] == ["run.py"]
    assert result["pipes_output_to"] == "summary"
    assert "parameters" not in result and "read_only_entry" not in str(result)


@pytest.mark.parametrize("selector", [{"file": "references/method.md"}, {"ref": "method"}, {"ref": "method.md"}])
def test_reference_aliases_read_original_text_without_method_substitution(package, selector):
    result = read_baseline_package(package, {"name": "dupont", "args": "replacement", **selector})
    assert result["file"] == "references/method.md"
    assert "{{ company }} stays literal" in result["body"]


def test_manifest_and_script_are_readable_text_without_execution(package):
    manifest = read_baseline_package(package, {"name": "dupont", "file": "SKILL.md"})
    script = read_baseline_package(package, {"name": "dupont", "file": "scripts/run.py"})
    assert manifest["body"].startswith("---\nname: dupont")
    assert script["body"] == "raise RuntimeError('MUST_NOT_EXECUTE')"


@pytest.mark.parametrize(
    "file",
    [
        "../SKILL.md",
        "/SKILL.md",
        "references/../SKILL.md",
        "references/.hidden",
        "references//method.md",
        "references/binary.bin",
        "private.txt",
        "references/missing.md",
    ],
)
def test_only_safe_retained_text_selectors_are_readable(package, file):
    with pytest.raises(ValueError, match="reference_skill_baseline_read_unavailable"):
        read_baseline_package(package, {"name": "dupont", "file": file})


def test_argument_snapshot_is_detached_before_await_and_bounded():
    arguments = {"name": "dupont", "args": ["2026"]}
    snapshot = snapshot_skill_read_arguments(arguments)
    arguments["args"][0] = "changed"
    assert snapshot["args"] == ["2026"]
    for bad in (
        {"name": "dupont", "args": {"year": 2026}},
        {"name": "dupont", "args": [True]},
        {"name": "dupont", "args": "x" * 65536},
        {"name": "dupont", "file": True},
    ):
        with pytest.raises(ValueError, match="reference_skill_arguments_unavailable"):
            snapshot_skill_read_arguments(bad)


def test_argument_expansion_is_bounded_before_allocating_the_result(tmp_path, monkeypatch):
    from bowhead_skill import index

    root = _skill(tmp_path / "repeated")
    (root / "SKILL.md").write_text("---\nname: dupont\ndescription: SOP\n---\n" + "$ARGUMENTS" * 200)
    package = prepare_skill_package(root, resource=_ref(root), expected_name="dupont")
    original = index._ARG_PLACEHOLDER
    expanded = []

    class Observed:
        finditer = original.finditer

        def sub(self, *args):
            expanded.append(True)
            return original.sub(*args)

    monkeypatch.setattr(index, "_ARG_PLACEHOLDER", Observed())
    with pytest.raises(ValueError, match="reference_skill_baseline_read_unavailable"):
        read_baseline_package(package, {"name": "dupont", "args": "x" * 8192})
    assert expanded == []


@pytest.mark.parametrize("body,args,expected", [("$ARGUMENTS", "中文", "中文"), ("$0/${ARGUMENTS}", ["a b"], "a b/'a b'"), ("$9 ok", [], " ok")])
def test_exact_utf8_argument_budget_matches_existing_substitution(body, args, expected):
    from bowhead_skill.index import substitute_args

    assert substitute_args(body, args) == expected
    assert substitute_args(body, args, max_output_bytes=len(expected.encode())) == expected
    with pytest.raises(ValueError, match="skill_argument_output_limit_exceeded"):
        substitute_args(body, args, max_output_bytes=len(expected.encode()) - 1)

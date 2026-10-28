"""A "Run Code" citation links to its code block through viewer output aliases.

`code_block.output_aliases` is derived in the viewer projection (never persisted) and equals the
alias of the sources entry for the same output artifact. Python code blocks also record when the run
started (`ran_at`) so the citation drawer can show it next to the run result.
"""

from __future__ import annotations

from bowhead_core.canvas_composers.mappers import MapperInput, _map_code_block
from bowhead_core.source_access_projection import (
    CODE_BLOCK_OUTPUT_ALIASES_KEY,
    ViewerSourceAliasMap,
    merge_viewer_patch_preserving_provenance,
    project_sources_for_viewer,
)
from bowhead_core.types import IntentType, ToolInvocation


def _aliases() -> ViewerSourceAliasMap:
    return ViewerSourceAliasMap.from_root_secret("root-secret", workspace_id="ws_1", report_scope_id="ses_1")


SECTIONS = [
    {"id": "sec_sources", "kind": "sources", "entries": [{"artifact_id": "art_out", "label": "Run Code: q"}]},
    {
        "id": "sec_code",
        "kind": "code_block",
        "call_id": "call_1",
        "lang": "python",
        "code": "print(1)",
        "output_artifact_ids": ["art_out", "art_out", ""],
    },
]


def test_code_block_output_aliases_equal_the_sources_entry_alias() -> None:
    projected, _ = project_sources_for_viewer(SECTIONS, [], alias_map=_aliases(), access_by_identity={"art_out": True})
    entry_alias = projected[0]["entries"][0]["source_alias"]
    # Duplicates and invalid ids collapse; the raw ids stay as they were.
    assert projected[1][CODE_BLOCK_OUTPUT_ALIASES_KEY] == [entry_alias]
    assert CODE_BLOCK_OUTPUT_ALIASES_KEY not in SECTIONS[1]


def test_aliases_do_not_depend_on_other_sections_or_access() -> None:
    alone, _ = project_sources_for_viewer([SECTIONS[1]], [], alias_map=_aliases(), access_by_identity={})
    together, _ = project_sources_for_viewer(SECTIONS, [], alias_map=_aliases(), access_by_identity={"art_out": True})
    assert alone[0][CODE_BLOCK_OUTPUT_ALIASES_KEY] == together[1][CODE_BLOCK_OUTPUT_ALIASES_KEY]


def test_a_stored_alias_value_is_replaced_by_the_derived_one() -> None:
    forged = {**SECTIONS[1], CODE_BLOCK_OUTPUT_ALIASES_KEY: ["source-ref-v1:forged"]}
    projected, _ = project_sources_for_viewer([forged], [], alias_map=_aliases(), access_by_identity={})
    assert "source-ref-v1:forged" not in projected[0][CODE_BLOCK_OUTPUT_ALIASES_KEY]
    # Without outputs nothing is derived, and a stored value is still never passed through.
    no_outputs = {**forged, "output_artifact_ids": []}
    projected, _ = project_sources_for_viewer([no_outputs], [], alias_map=_aliases(), access_by_identity={})
    assert CODE_BLOCK_OUTPUT_ALIASES_KEY not in projected[0]


def test_echoed_aliases_in_a_viewer_patch_are_never_persisted() -> None:
    projected, _ = project_sources_for_viewer([SECTIONS[1]], [], alias_map=_aliases(), access_by_identity={})
    merged = merge_viewer_patch_preserving_provenance(
        SECTIONS[1],
        projected[0],
        section_kind="code_block",
        section_id="sec_code",
        alias_map=_aliases(),
        access_by_identity={},
        allow_label_edit=False,
    )
    assert CODE_BLOCK_OUTPUT_ALIASES_KEY not in merged
    assert merged["output_artifact_ids"] == SECTIONS[1]["output_artifact_ids"]


def test_python_code_block_records_when_the_run_started() -> None:
    inv = ToolInvocation(
        tool_name="python_sandbox",
        arguments={"code": "print(1)"},
        result={"stdout": "1", "outputs": [{"artifact_id": "art_out"}]},
        started_at="2026-10-01T12:48:40Z",
    )
    out = _map_code_block(MapperInput(intent=IntentType.COMPUTE, question="", tool_invocations=[inv], turn_id="t_1"))
    assert out[0]["ran_at"] == "2026-10-01T12:48:40Z"

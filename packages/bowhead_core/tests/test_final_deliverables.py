"""Revision selection requires an exact input reference and a successful native output."""

from __future__ import annotations

import json

import pytest
from bowhead_core.final_deliverables import superseded_deliverable_ids
from bowhead_core.subagent import _extract_step_code_input


def revision_events(*, inputs: list[str], output: str = "art_final", state: str = "success", historical: bool = False) -> list[tuple[str, dict]]:
    arguments = {"code": "print('revision')", "inputs": {str(i): {"artifact_id": value} for i, value in enumerate(inputs)}}
    code_input = {"language": "python", "code": "print('revision')", "truncated": False}
    if not historical:
        code_input["input_artifact_ids"] = inputs
    return [
        ("step_started", {"step_id": output, "tool_name": "python_sandbox", "code_input": code_input, "arguments_preview": json.dumps(arguments)}),
        (
            "step_done",
            {
                "step_id": output,
                "state": state,
                "result_meta": {"output_artifacts": [{"artifact_id": output, "filename": "fibonacci.html", "mime": "text/html"}]},
            },
        ),
    ]


@pytest.mark.parametrize("historical", [False, True])
def test_exact_revision_replaces_only_its_input(historical: bool) -> None:
    identities = dict.fromkeys(("art_draft", "art_final", "art_unrelated"), ("fibonacci.html", "text/html"))
    assert superseded_deliverable_ids(identities, revision_events(inputs=["art_draft"], historical=historical)) == {"art_draft"}


@pytest.mark.parametrize("state", ["failed", "partial", "cancelled"])
def test_failed_or_partial_revision_does_not_replace_the_delivered_file(state: str) -> None:
    identities = dict.fromkeys(("art_draft", "art_final"), ("fibonacci.html", "text/html"))
    assert superseded_deliverable_ids(identities, revision_events(inputs=["art_draft"], state=state)) == set()


@pytest.mark.parametrize("inputs", [[], ["art_draft", "art_other"], ["art_unknown"]])
def test_same_names_without_an_unambiguous_input_are_preserved(inputs: list[str]) -> None:
    identities = dict.fromkeys(("art_draft", "art_other", "art_final"), ("fibonacci.html", "text/html"))
    assert superseded_deliverable_ids(identities, revision_events(inputs=inputs)) == set()


def test_revision_chain_keeps_only_its_final_output() -> None:
    identities = dict.fromkeys(("art_draft", "art_middle", "art_final"), ("fibonacci.html", "text/html"))
    events = revision_events(inputs=["art_draft"], output="art_middle") + revision_events(inputs=["art_middle"])
    assert superseded_deliverable_ids(identities, events) == {"art_draft", "art_middle"}


@pytest.mark.parametrize("identity", [("another.html", "text/html"), ("fibonacci.html", "text/plain")])
def test_deriving_a_different_file_does_not_supersede_the_input(identity: tuple[str, str]) -> None:
    assert (
        superseded_deliverable_ids({"art_draft": identity, "art_final": ("fibonacci.html", "text/html")}, revision_events(inputs=["art_draft"]))
        == set()
    )


def test_truncated_historical_arguments_do_not_prove_a_revision() -> None:
    events = revision_events(inputs=["art_draft"], historical=True)
    events[0][1]["arguments_preview"] = '{"inputs":'
    assert superseded_deliverable_ids(dict.fromkeys(("art_draft", "art_final"), ("fibonacci.html", "text/html")), events) == set()


def test_native_code_metadata_retains_exact_inputs_without_inspecting_source() -> None:
    projected = _extract_step_code_input("python_sandbox", {"input": {"code": "print('art_fake')", "inputs": {"src": {"artifact_id": "art_draft"}}}})
    assert projected is not None
    assert projected["input_artifact_ids"] == ["art_draft"]

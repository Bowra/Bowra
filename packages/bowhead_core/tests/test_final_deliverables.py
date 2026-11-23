"""Revision selection requires an exact input reference and a successful native output."""

from __future__ import annotations

import json

import pytest
from bowhead_core.final_deliverables import rewritten_deliverable_ids, superseded_deliverable_ids, unselected_sandbox_file_ids
from bowhead_core.subagent import _extract_step_code_input, _output_artifact_meta


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


def rewrite_events(
    *writes: tuple[str, str], state: str = "success", filename: str = "game.html", paths: tuple[str, ...] = ()
) -> list[tuple[str, dict]]:
    """One sandbox step per (helper, artifact) write, in order, each without inputs."""
    events: list[tuple[str, dict]] = []
    for index, (helper, artifact_id) in enumerate(writes):
        step_id = f"step_{index}"
        events.append(("step_started", {"step_id": step_id, "tool_name": "python_sandbox", "subagent_id": helper}))
        output = {"artifact_id": artifact_id, "filename": filename, "mime": "text/html"}
        if paths:
            output["path"] = paths[index]
        events.append(("step_done", {"step_id": step_id, "state": state, "result_meta": {"output_artifacts": [output]}}))
    return events


def test_a_helper_rewriting_one_file_keeps_only_its_newest_copy() -> None:
    # Observed case: one compute helper wrote dragonball_kinto_un.html six times, and the answer showed six cards.
    identities = dict.fromkeys(("art_1", "art_2", "art_3"), ("game.html", "text/html"))
    events = rewrite_events(("sub_compute", "art_1"), ("sub_compute", "art_2"), ("sub_compute", "art_3"))
    assert rewritten_deliverable_ids(identities, events) == {"art_1", "art_2"}


@pytest.mark.parametrize("state", ["failure", "partial"])
def test_the_newest_copy_counts_whatever_its_call_reported(state: str) -> None:
    # Observed case: every call that wrote pomodoro/index.html reported failure for another file.
    identities = dict.fromkeys(("art_1", "art_2"), ("game.html", "text/html"))
    assert rewritten_deliverable_ids(identities, rewrite_events(("sub_compute", "art_1"), ("sub_compute", "art_2"), state=state)) == {"art_1"}


def test_same_name_files_of_different_declared_folders_stay() -> None:
    # A nested delivery: one helper writes site/index.html, then site/blog/index.html in a later call.
    identities = dict.fromkeys(("art_home", "art_blog", "art_home_2"), ("index.html", "text/html"))
    events = rewrite_events(
        ("sub_compute", "art_home"),
        ("sub_compute", "art_blog"),
        ("sub_compute", "art_home_2"),
        filename="index.html",
        paths=("site/index.html", "site/blog/index.html", "site/index.html"),
    )
    assert rewritten_deliverable_ids(identities, events) == {"art_home"}


def test_step_metadata_keeps_the_declared_nested_path() -> None:
    meta = _output_artifact_meta(
        {"output_artifacts": [{"artifact_id": "art_1", "name": "index.html", "mime": "text/html", "path": "site/blog/index.html"}]}
    )
    assert meta == [{"artifact_id": "art_1", "filename": "index.html", "mime": "text/html", "path": "site/blog/index.html"}]


def test_same_name_files_of_different_helpers_stay() -> None:
    identities = dict.fromkeys(("art_east", "art_west"), ("game.html", "text/html"))
    assert rewritten_deliverable_ids(identities, rewrite_events(("sub_east", "art_east"), ("sub_west", "art_west"))) == set()


def test_files_without_a_reporting_step_or_with_another_identity_stay() -> None:
    identities = {"art_upload": ("game.html", "text/html"), "art_1": ("game.html", "text/html"), "art_text": ("game.html", "text/plain")}
    events = rewrite_events(("sub_compute", "art_1"))
    events += [
        ("step_started", {"step_id": "step_text", "tool_name": "python_sandbox", "subagent_id": "sub_compute"}),
        (
            "step_done",
            {
                "step_id": "step_text",
                "state": "success",
                "result_meta": {"output_artifacts": [{"artifact_id": "art_text", "filename": "game.html", "mime": "text/plain"}]},
            },
        ),
    ]
    assert rewritten_deliverable_ids(identities, events) == set()


def test_a_reported_name_that_differs_from_the_stored_file_proves_nothing() -> None:
    identities = dict.fromkeys(("art_1", "art_2"), ("game.html", "text/html"))
    events = rewrite_events(("sub_compute", "art_1")) + rewrite_events(("sub_compute", "art_2"), filename="other.html")
    events[2][1]["step_id"] = events[3][1]["step_id"] = "step_later"
    assert rewritten_deliverable_ids(identities, events) == set()


def sandbox_write(step_id: str, *written: str, declared: tuple[str, ...] = (), tool: str = "python_sandbox") -> list[tuple[str, dict]]:
    meta: dict = {"output_artifacts": [{"artifact_id": artifact_id} for artifact_id in written]}
    if declared:
        meta["artifact_presentation"] = {"origin": "producer_declared", "items": [{"artifact_id": artifact_id} for artifact_id in declared]}
    return [("step_started", {"step_id": step_id, "tool_name": tool}), ("step_done", {"step_id": step_id, "result_meta": meta})]


def presented(step_id: str, *artifact_ids: str) -> list[tuple[str, dict]]:
    meta = {"artifact_presentation": {"origin": "explicit_tool", "items": [{"artifact_id": artifact_id} for artifact_id in artifact_ids]}}
    return [("step_started", {"step_id": step_id, "tool_name": "present_artifacts"}), ("step_done", {"step_id": step_id, "result_meta": meta})]


def test_a_draft_beside_a_declared_file_leaves_the_answer() -> None:
    """Observed execution: fix.pptx, a draft from a failed call, showed beside the declared deck."""
    roots = {"art_deck": "art_deck", "art_fix": "art_fix"}
    events = sandbox_write("s1", "art_deck", declared=("art_deck",)) + sandbox_write("s2", "art_fix")
    assert unselected_sandbox_file_ids(roots, events) == {"art_fix"}


def test_a_turn_that_selected_nothing_keeps_every_file() -> None:
    roots = {"art_a": "art_a", "art_b": "art_b"}
    assert unselected_sandbox_file_ids(roots, sandbox_write("s1", "art_a") + sandbox_write("s2", "art_b")) == set()


def test_presenting_any_version_selects_the_file() -> None:
    """The Lead presented v1 of the deck; v16 and the undeclared deck it presented stay, the probe file leaves."""
    roots = {"art_v16": "art_v1", "art_probe": "art_probe", "art_undeclared": "art_undeclared"}
    events = sandbox_write("s1", "art_v16", "art_probe", "art_undeclared") + presented("p1", "art_v1", "art_undeclared")
    assert unselected_sandbox_file_ids(roots, events) == {"art_probe"}


def test_files_of_other_producers_always_stay() -> None:
    """Canvas exports and render Tools have no declaration step."""
    roots = {"art_deck": "art_deck", "art_export": "art_export", "art_unreported": "art_unreported"}
    events = sandbox_write("s1", "art_deck", declared=("art_deck",)) + sandbox_write("s2", "art_export", tool="skill_invoke")
    assert unselected_sandbox_file_ids(roots, events) == set()

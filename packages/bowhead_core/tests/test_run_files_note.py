"""The run's result files and work files, stated to the model after each save.

docs/sprints/pptx-upload-edit-dogfood-2026-11-23/spike.md ("can the model label result files and work files"):
a helper kept its deck in work_outputs across six saves and returned "已产出新文件"; the user got no file.
"""

from __future__ import annotations

import asyncio

from bowhead_core.run_files import run_files, run_files_note, saved_files
from bowhead_core.types import ToolInvocation
from test_agent_collaboration_runner import RunnerFixture
from test_agent_collaboration_runner import collab as collab

from bowhead_router import LLMResponse, ToolCall

DECK = "QBR.pptx"


def _save(arguments, outputs, *, tool="python_exec", error=None):
    return ToolInvocation(
        tool_name=tool,
        arguments=arguments,
        result=None if error else {"ok": True, "output_artifacts": outputs},
        error=error,
        duration_ms=1,
    )


def _out(name, artifact_id="art_1", **extra):
    return {"artifact_id": artifact_id, "name": name, **extra}


def test_work_files_and_result_files_are_listed_apart_and_a_later_delivery_counts_once() -> None:
    work = _save({"purpose": "analysis", "work_outputs": [DECK]}, [_out(DECK)])
    stdout = _save({"purpose": "analysis"}, [_out("stdout.txt", data_role="stdout")])
    assert run_files([work, work, stdout]) == ([], [DECK])
    delivered = _save({"purpose": "deliverable", "deliverable_outputs": [f"build/{DECK}"]}, [_out(DECK, "art_2", path=f"build/{DECK}")])
    assert run_files([work, delivered, work]) == ([DECK], [])


def test_the_name_the_user_sees_is_listed_and_failed_or_empty_calls_save_nothing() -> None:
    copy = _save({"purpose": "deliverable", "deliverable_outputs": [DECK]}, [_out(DECK, saved_as="QBR（已修改）.pptx")], tool="python_sandbox")
    assert run_files([copy]) == (["QBR（已修改）.pptx"], [])
    failed = _save({"purpose": "deliverable", "deliverable_outputs": [DECK]}, [], error="office package check failed")
    nothing = _save({"purpose": "analysis"}, [])
    other = _save({"purpose": "deliverable", "deliverable_outputs": [DECK]}, [_out(DECK)], tool="bi_query")
    assert not saved_files([failed, nothing, other])
    assert saved_files([copy])


def test_the_note_states_what_reaches_the_user_and_that_work_files_are_checked_alike() -> None:
    note = run_files_note([_save({"work_outputs": [DECK]}, [_out(DECK)])])
    assert note.startswith("Result files the user will see from this run: none. ")
    assert f"not shown to the user: {DECK}." in note
    assert note.endswith("Work files are saved and checked the same way as result files; only result files are shown to the user.")
    many = [_save({"work_outputs": [f"f{i}.csv"]}, [_out(f"f{i}.csv", f"art_{i}")]) for i in range(14)]
    assert "f11.csv (+2 more)" in run_files_note(many)


def test_the_run_files_note_is_turn_local() -> None:
    from bowhead_core.context_hygiene import MESSAGE_KIND_POLICY
    from bowhead_router.runtime_notes import RUN_FILES_NOTE_NAME, model_messages
    from bowhead_router.types import Message

    assert MESSAGE_KIND_POLICY[RUN_FILES_NOTE_NAME] == "turn_local"
    projected = model_messages([Message(role="user", name=RUN_FILES_NOTE_NAME, content="Result files …")])
    assert projected[0].name is None and projected[0].content == "[Bowhead runtime note — not a message from the user.]\nResult files …"


async def test_each_save_is_followed_by_the_runs_files_and_other_calls_are_not(collab, monkeypatch) -> None:
    """Mutation: drop the note from the loop, or list work files as result files → this test fails."""
    import bowhead_core.run_files as run_files_module
    from bowhead_router.runtime_notes import RUN_FILES_NOTE_NAME

    # The fixture's Agent may call only "probe" and the collaboration tools; "probe" stands in for python_exec.
    monkeypatch.setattr(run_files_module, "_SAVING_TOOLS", frozenset({"probe"}))

    seen: list[list[str]] = []
    calls = [
        ToolCall(id="c1", name="probe", arguments={"code": "x", "purpose": "analysis", "work_outputs": [DECK]}),
        ToolCall(id="c2", name="probe", arguments={"code": "x", "purpose": "analysis"}),
        ToolCall(id="c3", name="probe", arguments={"code": "x", "purpose": "deliverable", "deliverable_outputs": [DECK]}),
    ]

    async def probe(args, ctx):
        if args.get("purpose") == "analysis" and not args.get("work_outputs"):
            return {"ok": True, "rows": 3}
        return {"ok": True, "output_artifacts": [_out(DECK, f"art_{len(seen)}")]}

    async def provider(request):
        seen.append([m.content for m in request.messages if m.role == "user" and m.name == RUN_FILES_NOTE_NAME])
        step = len(seen) - 1
        if step < len(calls):
            return LLMResponse(content="", tool_calls=[calls[step]])
        return LLMResponse(content="Done.")

    harness = RunnerFixture(collab, provider)
    harness.register("probe", probe)
    try:
        result = await asyncio.wait_for(harness.run(collab.lead, task="Edit the deck."), 15)
        assert not harness.provider.failures, harness.provider.failures
        assert result.ok
        assert len(seen[1]) == 1 and seen[1][0].startswith(
            f"Result files the user will see from this run: none. Work files saved in this run, kept for later calls and not shown to the user: {DECK}."
        )
        assert seen[2] == seen[1], "A call that saved nothing adds no note"
        assert len(seen[3]) == 2 and seen[3][1].startswith(
            f"Result files the user will see from this run: {DECK}. Work files saved in this run, kept for later calls and not shown to the user: none."
        )
    finally:
        await harness.bus.close()

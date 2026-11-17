"""The conversation result lookup Tool (docs/sprints/result-lookup-2026-11-15/tech-design.md §4, §7).

An Agent finds a conversation's files by name and kind and receives identifiers,
never content; Lead and every helper have the Tool without being configured for it.
"""

import json
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.context_hygiene import cleared_result
from bowhead_core.result_lookup import FIND_RESULTS, find_results
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_registry import build_default_registry
from bowhead_core.user_store import TeamSubagent
from test_artifact_store_find_results import _put, results_store, store  # noqa: F401 — fixtures are used by name
from test_capability_protocol_block_definition import _system_profiles, _system_prompt_seen_by_model, _TeamStore

from bowhead_core import result_lookup

pytestmark = pytest.mark.asyncio


def _ctx(artifact_store, **more):
    return SimpleNamespace(session_id="s1", user_id="u", turn_id="t3", artifact_store=artifact_store, canvas_store=None, **more)


async def test_the_default_answer_lists_delivered_and_uploaded_files_with_what_to_do_next(store) -> None:  # noqa: F811
    report = await _put(store, "store-15-report.html", turn="t2")
    await _put(store, "rows.csv", kind="sandbox_output")
    upload = await _put(store, "budget.xlsx", turn=None, kind="user_upload", source="upload")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO turn_attachments(turn_id, artifact_id, seq) VALUES ('t1', ?, 0)", (upload.id,))
        await db.commit()

    answer = await find_results({}, _ctx(store))

    assert (answer["ok"], answer["shown"], answer["total"]) == (True, 2, 2)
    rows = {row["name"]: row for row in answer["results"]}
    assert rows["store-15-report.html"] == {
        "id": report.id,
        "name": "store-15-report.html",
        "kind": "delivered_file",
        "type": "html",
        "turn": 2,
        "age": "0 min ago",
        "made_by": "python_sandbox",
    }
    assert (rows["budget.xlsx"]["kind"], rows["budget.xlsx"]["turn"]) == ("uploaded_file", 1)
    assert set(answer["use"]) == {"delivered_file", "uploaded_file"}
    assert "not instructions" in answer["note"]
    # The unfiltered listing leaves data files out; their kind lists them, and a name narrows.
    assert [row["name"] for row in (await find_results({"kind": "data_file"}, _ctx(store)))["results"]] == ["rows.csv"]
    assert [row["id"] for row in (await find_results({"name": "store-15"}, _ctx(store)))["results"]] == [report.id]


async def test_a_name_alone_searches_every_file_of_the_conversation(store) -> None:  # noqa: F811
    """116, 2026-11-16: Lead asked for a data file by name without a kind and was told that nothing matched."""
    await _put(store, "report.html", turn="t2")
    work = await _put(store, "metric-037.csv", turn="t2", kind="sandbox_output")
    extract = await _put(store, "metric-sales_7dcc6a5.csv", turn="t2", kind="tool_data", source="bi_fetch_rows")
    await _put(store, "sandbox-stdout.txt", kind="sandbox_output", producer_kind="python_sandbox_stdout_v1")

    rows = (await find_results({"name": "metric-037"}, _ctx(store)))["results"]
    assert [(row["id"], row["kind"]) for row in rows] == [(work.id, "data_file")]
    answer = await find_results({"name": "METRIC"}, _ctx(store))
    assert {row["id"] for row in answer["results"]} == {work.id, extract.id} and answer["use"] == {"data_file": "pass the id as a sandbox input"}
    # A kind still narrows a named search, and a file the Results panel hides is found under no input.
    assert (await find_results({"name": "metric", "kind": "delivered_file"}, _ctx(store)))["total"] == 0
    assert (await find_results({"name": "stdout"}, _ctx(store)))["total"] == 0
    # With no input the listing is still delivered and uploaded files only.
    assert [row["name"] for row in (await find_results({}, _ctx(store)))["results"]] == ["report.html"]


async def test_an_empty_answer_names_what_was_searched(store) -> None:  # noqa: F811
    await _put(store, "rows.csv", kind="sandbox_output")
    unfiltered = await find_results({}, _ctx(store))
    # Data files exist: the empty default listing must not read as an empty conversation.
    assert unfiltered["total"] == 0 and "no delivered or uploaded file" in unfiltered["none"] and "kind" in unfiltered["none"]
    named = await find_results({"name": "missing"}, _ctx(store))
    assert "No file in this conversation has a name like that" in named["none"]
    by_kind = await find_results({"kind": "delivered_file"}, _ctx(store))
    assert by_kind["none"].startswith("Nothing in this conversation matches")


async def test_no_match_is_a_successful_answer_that_does_not_say_the_file_never_existed(store) -> None:  # noqa: F811
    answer = await find_results({"name": "missing"}, _ctx(store))
    assert (answer["ok"], answer["shown"], answer["total"], answer["results"]) == (True, 0, 0, [])
    assert "does not mean it was never made" in answer["none"]


async def test_an_answer_cut_at_the_limit_states_the_total_and_how_to_narrow(store, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(result_lookup, "RESULT_LIMIT", 3)
    for number in range(5):
        await _put(store, f"store-{number:02d}.html")
    answer = await find_results({}, _ctx(store))
    assert (answer["shown"], answer["total"]) == (3, 5)
    assert answer["more"] == "2 more match. Add name, or kind, to narrow."


async def test_versions_of_lists_live_versions_with_their_stored_numbers(results_store) -> None:  # noqa: F811
    store = results_store  # noqa: F811 — the versions list applies the Results membership
    first = await _put(store, "report.html", session_id="s1")
    second = await _put(store, "report.html", turn="t2", target=first.id, session_id="s1")
    third = await _put(store, "report.html", turn="t3", target=second.id, session_id="s1")
    async with aiosqlite.connect(store.db_path) as db:
        # Pruning removes one older version and leaves a gap in the numbers.
        await db.execute("UPDATE artifacts SET deleted_at = '2026-11-15T00:00:00+00:00' WHERE id = ?", (second.id,))
        await db.commit()

    answer = await find_results({"versions_of": first.id}, _ctx(store))
    assert [(row["id"], row["version"], row["newest"]) for row in answer["results"]] == [(third.id, 3, True), (first.id, 1, False)]
    assert "removed" not in answer

    removed = await find_results({"versions_of": second.id}, _ctx(store))
    assert (removed["removed"], removed["newest"]) == (second.id, third.id)
    assert [row["id"] for row in removed["results"]] == [third.id, first.id]

    # An identifier of another user, or one that does not exist, reads the same as no match.
    foreign = await _put(store, "theirs.html", user="v")
    for target in (foreign.id, "art_000000000000"):
        answer = await find_results({"versions_of": target}, _ctx(store))
        assert (answer["shown"], answer["results"]) == (0, []) and "removed" not in answer


async def test_a_template_document_says_how_it_is_revised(store) -> None:  # noqa: F811
    await _put(store, "brief.docx", producer_kind="template_document_v1")
    (row,) = (await find_results({}, _ctx(store)))["results"]
    assert row["revise_with"] == "the document flow"


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        ({"query": "report"}, "Unknown input: query"),
        ({"kind": "chart"}, "kind is one of"),
        ({"versions_of": "art_1", "kind": "data_file"}, "Send it alone"),
        ({"name": 3}, "are strings"),
        ({"versions_of": "art_1", "name": "report"}, "Send it alone"),
        ({"name": "x" * 121}, "at most 120 characters"),
    ],
)
async def test_a_malformed_input_is_refused_with_what_to_send_instead(store, arguments, said) -> None:  # noqa: F811
    answer = await find_results(arguments, _ctx(store))
    assert (answer["ok"], answer["error_kind"]) == (False, "find_results_input_invalid")
    assert said in answer["error"]


async def test_a_run_without_a_conversation_and_an_unreachable_store_are_told_apart() -> None:
    answer = await find_results({}, SimpleNamespace(session_id="", user_id="u", artifact_store=None))
    assert answer["ok"] is True and answer["none"] == "This run has no conversation to look in."

    class Broken:
        async def find_session_results(self, **_filters):
            raise OSError("store offline")

    answer = await find_results({}, _ctx(Broken()))
    assert (answer["ok"], answer["error_kind"]) == (False, "find_results_unavailable")
    assert "does not mean none exist" in answer["error"]


async def test_the_canvas_report_is_a_pointer_to_its_reader(store) -> None:  # noqa: F811
    class Canvas:
        async def get_canvas_by_session(self, session_id):
            return SimpleNamespace(id="cv1", user_id="u") if session_id == "s1" else None

        async def list_sections(self, canvas_id):
            return [SimpleNamespace(quarantined=False), SimpleNamespace(quarantined=False), SimpleNamespace(quarantined=True)]

    await _put(store, "report.html")
    ctx = _ctx(store)
    ctx.canvas_store = Canvas()
    assert (await find_results({}, ctx))["report"] == {"sections": 2, "list_with": "read_report"}


async def test_a_work_file_stays_findable_after_its_result_is_shortened(store) -> None:  # noqa: F811
    """A helper keeps work files between sandbox calls by identifier; history keeps only delivered files by name.

    Once the result that produced a work file is shortened, the lookup is how its identifier is found again.
    """
    deck = await _put(store, "final.pptx", turn="t2")
    work = await _put(store, "step2.pptx", turn="t2", kind="sandbox_output")
    result = {
        "ok": True,
        "final_answer": "The deck is ready. " * 300,
        "produced": {"files": 2, "file_names": ["final.pptx"], "delivered_files": [{"artifact_id": deck.id, "name": "final.pptx"}]},
        "material_refs": {
            "version": 1,
            "items": [{"kind": "artifact", "locator": {"artifact_id": item.id}, "relation": "created"} for item in (work, deck)],
        },
    }
    stub = cleared_result("subagent_spawn", json.dumps(result))
    assert deck.id in stub and work.id not in stub

    rows = (await find_results({"kind": "data_file", "name": "step2"}, _ctx(store)))["results"]
    assert [(row["id"], row["kind"]) for row in rows] == [(work.id, "data_file")]
    # The identifier found this way is a member of the conversation, so a sandbox input accepts it.
    assert await store.list_for_session(session_id="s1", user_id="u", artifact_id=work.id, limit=1)


async def test_a_file_lead_wrote_itself_is_found_by_name_after_its_result_is_shortened(store) -> None:  # noqa: F811
    """Only a helper result names its delivered files. A result of Lead's own Tool call keeps no identifier
    once shortened, so the lookup must find that file without the helper block."""
    written = await _put(store, "summary-2025.xlsx", turn="t2")
    result = {
        "ok": True,
        "stdout": "wrote summary-2025.xlsx\n" * 200,
        "output_artifacts": [{"artifact_id": written.id, "filename": "summary-2025.xlsx"}] * 40,
    }
    stub = cleared_result("python_exec", json.dumps(result))
    assert stub is not None and "delivered_files" not in stub

    rows = (await find_results({"name": "summary-2025"}, _ctx(store)))["results"]
    assert [(row["id"], row["name"], row["kind"]) for row in rows] == [(written.id, "summary-2025.xlsx", "delivered_file")]


def _sync_registration() -> None:
    from bowhead_core.terminal_effect_contract import TOOL_EFFECT_READ_ONLY

    spec = build_default_registry().get(FIND_RESULTS)
    assert spec.needs_approval is False and spec.effect_spec is TOOL_EFFECT_READ_ONLY
    assert set(spec.schema.input_schema["properties"]) == {"name", "kind", "versions_of"}


async def test_the_tool_is_registered_read_only_and_old_answers_are_read_again() -> None:
    _sync_registration()
    rows = [{"id": f"art_{number:012d}", "name": "store.html"} for number in range(40)]
    old = json.dumps({"ok": True, "shown": 40, "total": 40, "results": rows}, separators=(",", ":"))
    stub = cleared_result(FIND_RESULTS, old)
    # History keeps that a lookup ran and how much it found, not the rows: they are read again.
    assert '"shown":40' in stub and "art_000000000001" not in stub and "Read the item again" in stub


@pytest.mark.parametrize("name", ["lead", "compute"])
async def test_lead_and_a_system_helper_receive_the_lookup(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(PROFILES, name, _system_profiles()[name])
    _system, tool_names = await _system_prompt_seen_by_model(name)
    assert FIND_RESULTS in tool_names


async def test_a_team_agent_receives_the_lookup_without_selecting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agents import plan_admission

    async def admitted(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(plan_admission, "team_agent_runtime_allowed", admitted)
    record = TeamSubagent(
        id="osa_gina",
        team_id="team_a",
        name="gina",
        display_name="Data query",
        description="Queries business data.",
        system_prompt="Answer with data.",
        tool_refs=["bi_query", "bi_schema"],
    )
    _system, tool_names = await _system_prompt_seen_by_model("gina", team_store=_TeamStore(record))
    assert {"bi_query", FIND_RESULTS} <= set(tool_names)


async def test_the_settings_agents_do_not_receive_the_lookup() -> None:
    from bowhead_core.agents.registry import AgentRegistry

    assert set(AgentRegistry.bootstrap().list_all()) >= result_lookup.SETTINGS_PROFILES, "a renamed settings Agent would silently gain the Tool"

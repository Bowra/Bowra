"""Model-facing history hygiene: what a completed Lead Turn publishes for later Turns."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from bowhead_core.context_compaction import SUMMARY_MESSAGE_NAME, balanced_boundaries
from bowhead_core.context_hygiene import (
    CLEARED_RESULT_KIND,
    MESSAGE_KIND_POLICY,
    clear_old_tool_bodies,
    cleared_result,
    history_composition,
    is_cleared_result,
    omit_turn_local,
    published_history,
)
from bowhead_core.result_material_history import SUPPLEMENT_NAME
from bowhead_router.runtime_notes import (
    OUTPUT_RECOVERY_STATE_NAME,
    REJECTED_TOOL_SYNTAX_HISTORY_NAME,
    RUNTIME_CORRECTION_NOTE_NAME,
    model_messages,
)
from bowhead_router.types import Message, ToolCall

from bowhead_core import context_hygiene

pytestmark = pytest.mark.unit

GUIDANCE = "Fix requirement scope before filling evidence IDs. " * 100


def call(identity: str, name: str, arguments: dict | None = None) -> Message:
    return Message(role="assistant", content="", tool_calls=[ToolCall(id=identity, name=name, arguments=arguments or {})])


def result(identity: str, name: str, content: str | dict) -> Message:
    return Message(
        role="tool", name=name, tool_call_id=identity, content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    )


def material_items(index: int) -> list[dict]:
    """The model view of a helper's references: files it wrote, files it reused and sections it changed."""
    origin = {"kind": "tool", "turn_id": f"t_{index}", "producer_id": f"sub_{index}", "call_id": f"call_{index}"}
    files = [
        {"kind": "artifact", "locator": {"artifact_id": f"art_new_{index}"}, "relation": "created", "origin": origin},
        {"kind": "artifact", "locator": {"artifact_id": f"art_data_{index}"}, "relation": "updated", "origin": origin},
        {"kind": "artifact", "locator": {"artifact_id": f"art_new_{index}"}, "relation": "updated", "origin": origin},
    ]
    reused = [{"kind": "artifact", "locator": {"artifact_id": f"art_in_{index}_{n}"}, "relation": "reused", "origin": origin} for n in range(12)]
    sections = [
        {
            "kind": "canvas_section",
            "locator": {"canvas_id": "cv_1", "section_id": f"sec_{index}_{n}"},
            "observed_version": {"rev": n + 1},
            "relation": "updated",
            "origin": {"kind": "canvas_event", "turn_id": f"t_{index}", "event_seq": n},
        }
        for n in range(20)
    ]
    return [*files, *reused, *sections]


def helper_result(index: int) -> dict:
    """The measured shape of a helper result: little answer text, mostly repeated references."""
    return {
        "subagent_id": f"sub_{index}",
        "ok": True,
        "final_answer": "Section 3 revised.",
        "tokens_input": 48_211,
        "tokens_output": 2_904,
        "duration_ms": 91_337,
        "tool_execution": {"recorded": 12, "succeeded": 11, "failed": 1},
        "directive": "Tell the user which file changed. " * 30,
        "produced": {"files": 1, "file_names": [f"report_v{index}.html"]},
        "revised_files": {"targets": [{"artifact_id": f"art_old_{index}", "version": index}], "new_files": [{"artifact_id": f"art_new_{index}"}]},
        "payload_summary": {"title": "2025 review", "exec_summary": "Net revenue grew. " * 60},
        "material_refs": {"version": 1, "content_in_result": "omitted", "items": material_items(index)},
        "material_recovery_guidance": "Material references identify saved results. " * 8,
        "knowledge_provenance": {"schema_version": 1, "sources": {f"kbf_{index}": "g1"}},
        "knowledge_navigation_provenance": {f"kbf_{index}": {"path": "/retail", "generation": "g1"}},
    }


def turn(index: int, *, corrected: bool = False) -> list[Message]:
    messages = [
        Message(role="user", content=f"Revise the report, request {index}."),
        call(f"spawn{index}", "subagent_spawn", {"profile": "compute", "task": f"Revise section {index}."}),
        result(f"spawn{index}", "subagent_spawn", helper_result(index)),
    ]
    if corrected:
        messages += [
            Message(role="assistant", content="Done. Shall I continue?"),
            Message(role="user", name=RUNTIME_CORRECTION_NOTE_NAME, content=GUIDANCE),
            call(f"review{index}", "review_delivery", {"payload": {"delivery_review": {}}}),
            result(f"review{index}", "review_delivery", {"ok": True, "reviewed_completion": "complete", "instruction": "Answer in prose."}),
        ]
    return messages


def conversation(turns: int, *, corrected: bool = True) -> list[Message]:
    messages: list[Message] = []
    for index in range(1, turns + 1):
        messages += turn(index, corrected=corrected)
        if index < turns:
            # The final answer of the publishing Turn is appended by the publication itself.
            messages.append(Message(role="assistant", content=f"Revised section {index}; the file is report_v{index}.html."))
    return messages


def test_registered_kinds_match_the_constants_their_writers_use() -> None:
    assert MESSAGE_KIND_POLICY[RUNTIME_CORRECTION_NOTE_NAME] == "turn_local"
    assert MESSAGE_KIND_POLICY[REJECTED_TOOL_SYNTAX_HISTORY_NAME] == "turn_local"
    assert MESSAGE_KIND_POLICY[OUTPUT_RECOVERY_STATE_NAME] == "turn_local"
    assert MESSAGE_KIND_POLICY[SUPPLEMENT_NAME] == "turn_local"
    assert MESSAGE_KIND_POLICY[SUMMARY_MESSAGE_NAME] == "conversation"


def _named_message_kinds() -> dict[str, list[str]]:
    """Every name a non-Tool ``Message(...)`` is built with, resolved through module constants."""
    roots = [Path(__file__).parents[1] / "src", Path(__file__).parents[2] / "bowhead_router" / "src"]
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for root in roots for path in root.rglob("*.py")}
    constants: dict[str, set[str]] = {}
    aliases: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    constants.setdefault(target.id, set()).add(node.value.value)
                elif isinstance(node.value, ast.Name):
                    # STATE_NAME = OUTPUT_RECOVERY_STATE_NAME
                    aliases.setdefault(target.id, set()).add(node.value.id)
    for alias, originals in aliases.items():
        for original in originals:
            constants.setdefault(alias, set()).update(constants.get(original, ()))
    found: dict[str, list[str]] = {}
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "Message":
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            role, name = keywords.get("role"), keywords.get("name")
            if name is None or (isinstance(role, ast.Constant) and role.value == "tool") or "tool_call_id" in keywords:
                continue
            site = f"{path.name}:{node.lineno}"
            if isinstance(name, ast.Constant):
                values = {name.value} if isinstance(name.value, str) else set()
            elif isinstance(name, ast.Name) and constants.get(name.id):
                values = constants[name.id]
            else:
                values = {f"<dynamic {ast.unparse(name)}>"}
            for value in values:
                found.setdefault(value, []).append(site)
    return found


# Names chosen at run time. Each is listed with the registered kinds it can take.
DYNAMIC_NAME_SITES = {
    # A rejected final keeps None or the rejected-tool-syntax kind.
    "<dynamic correction.assistant_history_name>": {REJECTED_TOOL_SYNTAX_HISTORY_NAME},
    # One of the two attachment dependency kinds.
    "<dynamic name>": {"context_attachment_dependencies_v1", "context_attachment_dependencies_v2"},
}


def test_every_server_authored_message_kind_declares_its_history_policy() -> None:
    found = _named_message_kinds()
    assert RUNTIME_CORRECTION_NOTE_NAME in found, "the scan no longer sees the Runner's correction note"
    unregistered = {
        name: sites
        for name, sites in found.items()
        if name not in MESSAGE_KIND_POLICY and not (name in DYNAMIC_NAME_SITES and DYNAMIC_NAME_SITES[name] <= set(MESSAGE_KIND_POLICY))
    }
    assert not unregistered, (
        f"Register each new message kind in context_hygiene.MESSAGE_KIND_POLICY as turn_local, conversation or dependency: {unregistered}"
    )


def test_publication_omits_notes_the_answers_they_superseded_and_review_exchanges() -> None:
    messages = conversation(2)
    kept = omit_turn_local(messages)
    assert not any(message.name == RUNTIME_CORRECTION_NOTE_NAME for message in kept)
    assert not any(message.content == "Done. Shall I continue?" for message in kept)
    assert not any(tool.name == "review_delivery" for message in kept for tool in message.tool_calls or ())
    assert not any(message.role == "tool" and message.name == "review_delivery" for message in kept)
    # The conversation itself is intact: both requests, both helper exchanges and the first Turn's answer.
    assert [message.content for message in kept if message.role == "user"] == ["Revise the report, request 1.", "Revise the report, request 2."]
    assert sum(message.role == "tool" and message.name == "subagent_spawn" for message in kept) == 2
    assert any(message.content.startswith("Revised section 1") for message in kept)
    assert balanced_boundaries(kept)[-1] == len(kept)


def test_a_correction_after_refused_tool_calls_keeps_the_preceding_exchange() -> None:
    messages = [
        Message(role="user", content="Chart the stores."),
        call("c1", "add_chart"),
        result("c1", "add_chart", {"ok": False, "error_kind": "invalid_args"}),
        Message(role="user", name=RUNTIME_CORRECTION_NOTE_NAME, content="Do not call any Tool now. Answer the user."),
    ]
    kept = omit_turn_local(messages)
    assert kept == messages[:3]


def test_a_review_call_batched_with_business_work_stays_paired() -> None:
    mixed = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="r1", name="review_delivery", arguments={}), ToolCall(id="p1", name="present_artifacts", arguments={})],
    )
    messages = [
        Message(role="user", content="Deliver the file."),
        mixed,
        result("r1", "review_delivery", {"ok": False}),
        result("p1", "present_artifacts", {"ok": True}),
    ]
    assert omit_turn_local(messages) == messages


def test_omission_leaves_the_model_projection_of_other_messages_unchanged() -> None:
    messages = conversation(1)
    assert model_messages(omit_turn_local(messages)) == omit_turn_local(messages)


def test_a_stub_keeps_identity_and_outcome_and_drops_repeated_material() -> None:
    original = json.dumps(helper_result(3), ensure_ascii=False)
    stub = cleared_result("subagent_spawn", original)
    assert stub is not None and is_cleared_result(stub) and len(stub) < len(original) * 0.5
    data = json.loads(stub)
    assert data["cleared_result"] == CLEARED_RESULT_KIND and data["tool"] == "subagent_spawn" and data["original_chars"] == len(original)
    kept = data["kept"]
    assert kept["ok"] is True and kept["subagent_id"] == "sub_3" and kept["final_answer"] == "Section 3 revised."
    assert kept["revised_files"]["targets"] == [{"artifact_id": "art_old_3", "version": 3}]
    assert kept["produced"]["file_names"] == ["report_v3.html"]
    assert kept["payload_summary"]["exec_summary"].endswith("chars removed]")
    assert not {"material_refs", "material_recovery_guidance", "directive"} & set(kept)
    # The files this result wrote stay addressable; reused inputs and sections do not repeat.
    assert data["produced_artifacts"] == [{"artifact_id": "art_new_3", "relation": "created"}, {"artifact_id": "art_data_3", "relation": "updated"}]
    assert "art_in_3_0" not in stub and "sec_3_0" not in stub and "produced_artifacts_omitted" not in data
    # Run statistics are neither identity nor outcome.
    assert not {"tokens_input", "tokens_output", "duration_ms", "tool_execution"} & set(kept)
    # Source validation reads provenance at the top level of a result.
    assert data["knowledge_provenance"] == helper_result(3)["knowledge_provenance"]
    assert data["knowledge_navigation_provenance"] == helper_result(3)["knowledge_navigation_provenance"]
    assert "do not repeat it" in data["note"]
    # A stub is written once and never rewritten.
    assert cleared_result("subagent_spawn", stub) is None
    assert cleared_result("subagent_spawn", original) == stub


def test_a_stub_bounds_the_produced_files_it_names() -> None:
    origin = {"kind": "tool", "turn_id": "t_1", "producer_id": "sub_1", "call_id": "call_1"}
    items = [{"kind": "artifact", "locator": {"artifact_id": f"art_{n:02d}"}, "relation": "created", "origin": origin} for n in range(15)]
    body = {"ok": True, "final_answer": "x" * 4000, "material_refs": {"version": 1, "items": [*items, {"kind": "artifact"}, "bad"]}}
    data = json.loads(cleared_result("subagent_spawn", json.dumps(body)))
    assert [entry["artifact_id"] for entry in data["produced_artifacts"]] == [f"art_{n:02d}" for n in range(12)]
    assert data["produced_artifacts_omitted"] == 3
    # A result without its own reference list names nothing.
    assert "produced_artifacts" not in json.loads(cleared_result("subagent_spawn", json.dumps({"ok": True, "final_answer": "x" * 4000})))


def test_a_read_stub_keeps_coverage_and_asks_for_a_fresh_read() -> None:
    report = {
        "ok": True,
        "outline": [{"section_id": f"sec_{i}", "title": "Stores", "summary": "x" * 200} for i in range(14)],
        "sections": [{"section_id": "sec_1", "text": "y" * 3000}],
        "coverage": {"view": "outline", "complete": True, "snapshot": "v7"},
    }
    data = json.loads(cleared_result("read_report", json.dumps(report)))
    assert data["kept"] == {"ok": True, "coverage": {"view": "outline", "complete": True, "snapshot": "v7"}}
    assert "Read the item again" in data["note"]


def test_text_results_keep_a_short_preview() -> None:
    data = json.loads(cleared_result("web_fetch", "plain text " * 500))
    assert data["kept"] == {"preview": ("plain text " * 500)[:300]}


def test_clearing_runs_as_one_batch_only_when_it_pays_for_the_cache_miss() -> None:
    messages = conversation(6, corrected=False)
    # Below the minimum nothing changes, so the cached prefix stays valid.
    assert clear_old_tool_bodies(messages, keep_turns=2, min_tokens=10**9).messages is messages
    cleared = clear_old_tool_bodies(messages, keep_turns=2, min_tokens=1_000)
    assert cleared.results == 4 and cleared.tokens >= 1_000 and cleared.chars > 0
    starts = [index for index, message in enumerate(cleared.messages) if message.role == "user"]
    old, recent = cleared.messages[: starts[-2]], cleared.messages[starts[-2] :]
    assert all(is_cleared_result(message.content) for message in old if message.role == "tool")
    assert recent == messages[starts[-2] :]
    # Calls, arguments, requests and answers are never rewritten.
    assert [m for m in cleared.messages if m.role != "tool"] == [m for m in messages if m.role != "tool"]
    assert balanced_boundaries(cleared.messages)[-1] == len(cleared.messages)
    # The next Turn adds one eligible result: too little for a batch, and existing stubs are left byte-identical.
    grown = [*cleared.messages, Message(role="assistant", content="Revised section 6."), *turn(7)]
    again = clear_old_tool_bodies(grown, keep_turns=2, min_tokens=cleared.tokens)
    assert again.messages is grown


def test_clearing_keeps_recent_turns_small_results_and_protected_tools() -> None:
    skill = json.dumps({"ok": True, "instructions": "Always state the unit. " * 200})
    answer = json.dumps({"ok": True, "answer": "Use net revenue."})
    messages = [
        Message(role="user", content="Start."),
        call("s1", "skill_read"),
        result("s1", "skill_read", skill),
        call("q1", "clarification_ask"),
        result("q1", "clarification_ask", answer),
        call("p1", "present_artifacts"),
        result("p1", "present_artifacts", {"ok": True, "output_artifacts": [{"artifact_id": "art_1"}]}),
        Message(role="assistant", content="Started."),
        *conversation(3, corrected=False),
    ]
    cleared = clear_old_tool_bodies(messages, keep_turns=2, min_tokens=1)
    by_call = {message.tool_call_id: message.content for message in cleared.messages if message.role == "tool"}
    assert by_call["s1"] == skill and by_call["q1"] == answer
    assert not is_cleared_result(by_call["p1"])
    assert is_cleared_result(by_call["spawn1"])
    assert not is_cleared_result(by_call["spawn2"]) and not is_cleared_result(by_call["spawn3"])
    assert clear_old_tool_bodies(conversation(2, corrected=False), keep_turns=2, min_tokens=1).results == 0


def test_published_history_is_unchanged_while_the_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_CONTEXT_HISTORY_HYGIENE", raising=False)
    messages = conversation(6)
    published = published_history(messages)
    assert published.messages is messages and not published.enabled
    assert published.before == published.after and published.omitted_messages == 0 and published.cleared_results == 0


def test_published_history_applies_both_layers_and_reports_sizes_only(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("FEATURE_CONTEXT_HISTORY_HYGIENE", "1")
    monkeypatch.setenv("BOWHEAD_CTX_HYGIENE_MIN_SAVING_TOKENS", "1000")
    messages = conversation(6)
    published = published_history(messages)
    assert published.enabled and published.omitted_messages == 6 * 4 and published.cleared_results == 4
    assert published.after.notes == 0 and published.before.notes == 6 * len(GUIDANCE)
    assert published.after.chars < published.before.chars * 0.6
    assert published.after.cleared_results == 4
    with caplog.at_level("INFO", logger="bowhead_core.context_hygiene"):
        context_hygiene.record_publication(published, session_id="s_1", turn_id="t_1")
    line = caplog.messages[-1]
    assert "context_publication.composition session=s_1 turn=t_1 hygiene=on" in line
    assert f"before_chars={published.before.chars}" in line and "cleared_results=4" in line
    assert "Revise the report" not in line and "Net revenue" not in line


def test_composition_counts_each_kind_once() -> None:
    messages = conversation(1)
    composition = history_composition(messages)
    assert composition.messages == len(messages)
    assert composition.user == len("Revise the report, request 1.")
    assert composition.notes == len(GUIDANCE)
    assert composition.top_tools[0][0] == "subagent_spawn"
    assert composition.chars == composition.user + composition.assistant + composition.call_arguments + composition.tool_results + composition.notes

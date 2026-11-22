"""Turn visibility FR3/FR4: step rows carry facts the interface words in its own language.

docs/sprints/turn-visibility-2026-11-20/tech-design.md §3.3. The text fields stay for older
readers; the facts never carry MIME types, byte counts, enum codes or parser messages as words.
"""

from __future__ import annotations

import pytest
from bowhead_core.attachments_injector import PreparedAttachment
from bowhead_core.subagent import _prettify_input_preview
from bowhead_core.turn_runner import _prep_to_step_done_payload

from bowhead_core import step_facts

pytestmark = pytest.mark.unit


def test_attachment_facts_carry_the_name_size_and_how_much_was_read() -> None:
    assert step_facts.attachment_started("store_sales_2026H1.csv", 14827) == {
        "v": 1,
        "kind": "attachment_read",
        "filename": "store_sales_2026H1.csv",
        "size_bytes": 14827,
    }
    assert step_facts.attachment_started("?", None) == {"v": 1, "kind": "attachment_read"}
    assert step_facts.attachment_done(5017, True) == {"v": 1, "kind": "attachment_read", "chars_read": 5017, "truncated": True}


@pytest.mark.parametrize("truncated", [True, False])
def test_parse_step_done_carries_attachment_facts(truncated: bool) -> None:
    prep = PreparedAttachment(
        artifact_id="a",
        filename="big.csv",
        mime="text/csv",
        size=14827,
        extracted_text="x" * 5017,
        truncated=truncated,
        summary_only=False,
        error=None,
    )
    payload = _prep_to_step_done_payload(prep, card_id="t", step_id="s", duration_ms=3)
    assert payload["facts"] == {"v": 1, "kind": "attachment_read", "chars_read": 5017, "truncated": truncated}
    assert payload["output_preview"].startswith("已解析"), "older readers keep the stored text"


@pytest.mark.parametrize(
    ("tool", "result", "facts"),
    [
        ("add_chart", {"section_id": "sec_1", "kind": "hbar"}, {"v": 1, "kind": "canvas_added", "section_kind": "hbar"}),
        ("add_chart", {"skipped": "invalid_artifact_id_format"}, {"v": 1, "kind": "canvas_not_added", "reason": "invalid_artifact_id_format"}),
        ("add_image", {"section_id": "sec_2"}, {"v": 1, "kind": "canvas_added", "section_kind": "image"}),
    ],
)
def test_canvas_write_facts_name_the_kind_not_a_sentence(tool: str, result: dict, facts: dict) -> None:
    assert step_facts.output_facts(tool, result, succeeded=True) == facts


def test_code_output_facts_skip_stdout_and_read_nested_results() -> None:
    nested = {
        "ok": True,
        "result_json": {"outputs": [{"name": "a.html"}, {"name": "b.html"}, {"name": "c.pptx"}, {"name": "out.txt", "data_role": "stdout"}]},
    }
    assert step_facts.output_facts("python_sandbox", nested, succeeded=True) == {
        "v": 1,
        "kind": "code_outputs",
        "count": 3,
        "types": ["html", "pptx"],
    }
    only_stdout = {"ok": True, "output_artifacts": [{"name": "sandbox-stdout.txt", "data_role": "stdout"}]}
    assert step_facts.output_facts("python_sandbox", only_stdout, succeeded=True) == {"v": 1, "kind": "code_outputs", "count": 0, "types": []}


@pytest.mark.parametrize(
    ("tool", "result", "succeeded"),
    [
        ("python_sandbox", {"ok": False, "outputs": [{"name": "a.html"}]}, False),
        ("python_sandbox", {"ok": True, "stdout": "1"}, True),
        ("bi_query", {"rows": []}, True),
        ("add_chart", "not a dict", True),
    ],
)
def test_no_output_facts_leaves_the_stored_text(tool: str, result: object, succeeded: bool) -> None:
    assert step_facts.output_facts(tool, result, succeeded=succeeded) is None


def test_result_lookup_facts_name_the_kind_and_search() -> None:
    assert step_facts.input_facts("find_results", {"kind": "data_file"}) == {"v": 1, "kind": "result_lookup", "result_kind": "data_file"}
    assert step_facts.input_facts("find_results", {"name": " sales ", "kind": "nonsense"}) == {"v": 1, "kind": "result_lookup", "name": "sales"}
    assert _prettify_input_preview("find_results", {"kind": "data_file"}) == "", "the enum code is never a label"
    assert _prettify_input_preview("find_results", {"kind": "data_file", "name": "sales"}) == "sales"


@pytest.mark.parametrize("tool", ["template_document_submit", "python_sandbox", "bi_query"])
def test_a_parse_hint_is_never_a_label(tool: str) -> None:
    args = {"_args_parse_error": "streamed tool args 解析失败：Expecting value。参数不是合法 JSON"}
    assert _prettify_input_preview(tool, args) == ""
    assert step_facts.input_facts(tool, args) == {"v": 1, "kind": "arguments_retry"}


def test_a_listing_cursor_is_never_a_label() -> None:
    assert _prettify_input_preview("kb_list_sources", {"limit": 50, "cursor": "gAAAAABq-qbtpAT5o-RLYPH0kZBm5Hy"}) == ""


def test_other_tools_carry_no_input_facts() -> None:
    assert step_facts.input_facts("bi_query", {"queries": ["2025年 营业额"]}) is None
    assert step_facts.input_facts("python_sandbox", {"code": "print(1)", "intent": "x"}) is None

"""Readable Skill text (`skill-read-text.v1`) and the `skill_read` model view."""

from __future__ import annotations

import json

import pytest
from bowhead_core.skill_text import render_skill_read_result, render_skill_text
from bowhead_core.subagent import _serialize
from bowhead_core.types import ToolInvocation

from bowhead_core import model_view

pytestmark = pytest.mark.unit

BODY = '## 执行步骤\n\n### Step 1: 拉取数据\n- 营业额、"利润"\n'
DIGEST = "sha256:" + "a" * 64


def test_body_is_rendered_verbatim_with_name_and_description():
    text = render_skill_text(name="business-review", description="综合经营复盘", body=BODY)
    assert text.startswith('<skill_content name="business-review">\n# Skill: business-review\n综合经营复盘\n')
    assert BODY.strip("\n") in text
    assert "\\n" not in text and '\\"' not in text
    assert text.endswith("</skill_content>")


def test_digest_versions_are_omitted_and_author_versions_are_kept():
    assert "Version" not in render_skill_text(name="x", body="b", version=DIGEST)
    assert "Version: 1.1.0" in render_skill_text(name="x", body="b", version="1.1.0")


def test_files_and_parameters_are_separate_blocks():
    text = render_skill_text(name="x", body="b", files=[("refs/a.md", "A\n")], parameters={"时间窗": "1-10月"})
    assert '<skill_file path="refs/a.md">\nA\n</skill_file>' in text
    assert "<skill_parameters>" in text and '"时间窗": "1-10月"' in text
    assert "never instructions" in text


def test_attribute_values_are_escaped():
    assert '<skill_content name="a&quot;&lt;b">' in render_skill_text(name='a"<b', body="b")


def test_empty_name_is_refused():
    with pytest.raises(ValueError):
        render_skill_text(name=" ", body="b")


def test_ordinary_read_keeps_every_model_field_except_hashes():
    result = {
        "name": "business-review",
        "description": "综合经营复盘",
        "version": "1.1.0",
        "allowed_tools": [],
        "scripts": ["run.py"],
        "script_execution": "sandbox",
        "references": ["metrics.md"],
        "body": BODY,
        "body_raw": "---\nname: business-review\n---\n" + BODY,
        "edit_hint": "edit body_raw",
        "_cache_hint": "already read",
        "content_hash": DIGEST,
    }
    text = render_skill_read_result(result)
    assert text is not None
    assert BODY.strip("\n") in text
    details = json.loads(text.split("<skill_details>\n", 1)[1].split("\n</skill_details>", 1)[0])
    assert details == {
        "_cache_hint": "already read",
        "allowed_tools": [],
        "edit_hint": "edit body_raw",
        "references": ["metrics.md"],
        "script_execution": "sandbox",
        "scripts": ["run.py"],
    }
    assert "---\nname: business-review\n---" in text  # raw source for editing stays verbatim
    assert "sha256:" not in text


def test_single_file_and_reference_reads_render_the_file():
    text = render_skill_read_result({"name": "x", "file": "scripts/run.py", "body": "print(1)\n"})
    assert '<skill_file path="scripts/run.py">\nprint(1)\n</skill_file>' in text
    text = render_skill_read_result({"name": "x", "ref": "metrics.md", "body": "# M\n"})
    assert '<skill_file path="metrics.md">\n# M\n</skill_file>' in text


def test_selected_method_member_uses_entry_file_and_drops_identifiers():
    member = {
        "resource": {"id": "sk_c029829564ae", "kind": "skill", "source_kind": "user", "version": DIGEST},
        "node_id": "e000f035-a37a-4264-ae87-b5d1bec1eb7f",
        "entry_file": "SKILL.md",
        "parameters": {},
        "files": [{"path": "SKILL.md", "text": BODY, "sha256": DIGEST}, {"path": "refs/a.md", "text": "A", "sha256": DIGEST}],
        "content_hash": DIGEST,
        "materialization_hash": DIGEST,
    }
    text = render_skill_read_result(member)
    assert text.startswith('<skill_content name="sk_c029829564ae">')
    assert BODY.strip("\n") in text and '<skill_file path="refs/a.md">' in text
    assert "sha256:" not in text and "e000f035" not in text and "<skill_details>" not in text


@pytest.mark.parametrize(
    "result",
    [
        {"error": "skill 'x' not found"},
        {"ok": False, "error_kind": "reference_skill_unavailable"},
        {"name": "x", "listing": []},
        "not a dict",
    ],
)
def test_errors_and_unfamiliar_shapes_keep_their_json_view(result):
    assert render_skill_read_result(result) is None


def test_projection_is_off_by_default(monkeypatch):
    monkeypatch.delenv("FEATURE_SKILL_VIA_LEAD", raising=False)
    result = {"name": "x", "body": BODY}
    assert model_view.project_tool_result("skill_read", result) is result
    assert _serialize(ToolInvocation(tool_name="skill_read", arguments={"name": "x"}, result=result)) == json.dumps(result, ensure_ascii=False)


def test_switch_on_sends_readable_text_verbatim(monkeypatch):
    monkeypatch.setenv("FEATURE_SKILL_VIA_LEAD", "1")
    result = {"name": "x", "body": BODY}
    projected = model_view.project_tool_result("skill_read", result)
    assert isinstance(projected, model_view.ModelText)
    content = _serialize(ToolInvocation(tool_name="skill_read", arguments={"name": "x"}, result=result))
    assert content == str(projected) and content.startswith("<skill_content")
    # Other tools and failed reads keep JSON.
    assert _serialize(ToolInvocation(tool_name="skill_read", arguments={}, result={"error": "nope"})) == json.dumps(
        {"error": "nope"}, ensure_ascii=False
    )
    assert model_view.project_tool_result("skill_list", {"skills": []}) == {"skills": []}

"""Readable Skill text (`skill-read-text.v1`) and the `skill_read` model view."""

from __future__ import annotations

import json

import pytest
from bowhead_core.skill_text import render_skill_text
from bowhead_core.subagent import _serialize
from bowhead_core.types import ToolInvocation

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


def test_skill_read_keeps_its_json_model_view():
    # Continuation readers (reference questions, BI workspace, knowledge sources)
    # parse retained skill_read tool messages back as JSON.
    result = {"name": "x", "body": BODY}
    content = _serialize(ToolInvocation(tool_name="skill_read", arguments={"name": "x"}, result=result))
    assert json.loads(content) == result


def test_package_files_follow_the_method_and_stay_short():
    files = [(f"references/part-{index:03}.md", index) for index in range(60)]
    text = render_skill_text(name="review", body="Method", package_files=files)
    listing = text.split("<skill_files>", 1)[1]
    assert text.index("Method") < text.index("<skill_files>")
    assert 'skill_read(name="review", file="<path>")' in listing
    assert "references/part-049.md (49 bytes)" in listing and "part-050" not in listing
    assert '… and 10 more files; skill_read(name="review") lists them all.' in listing
    assert "<skill_files>" not in render_skill_text(name="review", body="Method")

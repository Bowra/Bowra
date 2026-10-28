"""Current template parsing and attachment detection contracts."""

import pytest
from bowhead_core.template_clone import parse_template


@pytest.mark.unit
def test_parse_template_unsupported_format():
    with pytest.raises(ValueError, match="unsupported"):
        parse_template("foo.docx")


class _FakePrepared:
    """模拟 attachments_injector 的 prepared payload."""

    def __init__(self, *, mime: str = "", filename: str = "", artifact_id: str = "", local_path: str = "/tmp/x") -> None:
        self.mime = mime
        self.filename = filename
        self.artifact_id = artifact_id
        self.local_path = local_path


@pytest.mark.unit
def test_template_attachment_paths_pdf():
    from bowhead_core.turn_runner import template_attachment_paths

    prep = [_FakePrepared(mime="application/pdf", filename="example-report.pdf", artifact_id="art_1", local_path="/tmp/m.pdf")]
    out = template_attachment_paths(prep)
    assert out == [("art_1", "/tmp/m.pdf")]


@pytest.mark.unit
def test_template_attachment_paths_pptx():
    from bowhead_core.turn_runner import template_attachment_paths

    prep = [_FakePrepared(filename="report.pptx", artifact_id="art_2", local_path="/tmp/r.pptx")]
    out = template_attachment_paths(prep)
    assert out == [("art_2", "/tmp/r.pptx")]


@pytest.mark.unit
def test_template_attachment_paths_skips_csv():
    from bowhead_core.turn_runner import template_attachment_paths

    prep = [_FakePrepared(filename="data.csv", artifact_id="art_3")]
    assert template_attachment_paths(prep) == []


@pytest.mark.unit
def test_user_wants_template_clone_positive():
    from bowhead_core.turn_runner import user_wants_template_clone

    assert user_wants_template_clone("用这个模板做这个月的")
    assert user_wants_template_clone("按这份做新一期")
    assert user_wants_template_clone("做 11 月的")
    assert user_wants_template_clone("use this template for this month")


@pytest.mark.unit
def test_user_wants_template_clone_negative():
    from bowhead_core.turn_runner import user_wants_template_clone

    assert not user_wants_template_clone("分析一下这份报告的数据")
    assert not user_wants_template_clone("这份 PDF 讲了啥")
    assert not user_wants_template_clone("")

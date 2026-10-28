"""Invalid deployment/team policy never silently removes resource boundaries."""

import pytest
from bowhead_core.html_preview_policy import DEFAULT_HTML_PREVIEW_BYTES, html_preview_limit_bytes
from bowhead_core.kb.retrieval_policy import retrieval_top_k


@pytest.mark.parametrize(
    "value,expected",
    [
        ("6291456", 6291456),
        ("52428800", 52428800),
        ("0", DEFAULT_HTML_PREVIEW_BYTES),
        ("52428801", DEFAULT_HTML_PREVIEW_BYTES),
        ("bad", DEFAULT_HTML_PREVIEW_BYTES),
    ],
)
def test_html_policy_is_bounded_and_explicit(monkeypatch, value, expected):
    monkeypatch.setenv("BOWHEAD_CANVAS_HTML_MAX_BYTES", value)
    assert html_preview_limit_bytes() == expected


def test_kb_default_and_configured_count():
    assert retrieval_top_k({}) == 15
    assert retrieval_top_k({"retrieval_top_k": 30}) == 30


@pytest.mark.parametrize("value", [0, 51, True, "15", 1.5, None])
def test_invalid_kb_count_names_the_field(value):
    with pytest.raises(ValueError, match=r"kb.retrieval_top_k"):
        retrieval_top_k({"retrieval_top_k": value})

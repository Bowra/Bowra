"""Explicit assignment sources survive output prose and continuation."""

import pytest
from bowhead_core.source_obligations import required_source_urls, retained_sources, task_envelope
from bowhead_router.types import Message

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("values", [None, "https://example.com", ["file:///tmp/a"], ["https://example.com/a b"], ["https://example.com"] * 17])
def test_invalid_source_declaration_is_rejected(values):
    with pytest.raises(ValueError):
        required_source_urls(values)


def test_declared_sources_are_independent_of_task_literals_and_roundtrip():
    sources = required_source_urls(["https://example.com/", "https://example.com"])
    task = '任意语言の例 <script src="https://example.invalid/offline.js"></script>'
    messages = [Message(role="user", content=task_envelope(task, sources))]
    assert sources == {"https://example.com"}
    assert retained_sources(messages) == sources
    assert retained_sources([Message(role="user", content=task_envelope(task, frozenset()))]) == frozenset()
    assert required_source_urls(["https://example.invalid/offline.js"]) == {"https://example.invalid/offline.js"}


def test_legacy_transcript_has_no_inferred_source_contract():
    assert retained_sources([Message(role="user", content="Read https://example.com")]) == frozenset()

"""Generated source labels follow the answer language as a whole.

The tool prefix ("Run Code" / "运行代码" …) used to follow the interface language while the
run-ordinal suffix ("（第 1 次运行）" / " (run 1)" …) followed the answer language, so one label could
mix two languages. Both now resolve through `source_label_language`, the same rule as the report chrome.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from bowhead_core.agents._generated.tool_display import TOOL_DISPLAY
from bowhead_core.agents._lead_context import (
    reset_current_output_language,
    reset_current_ui_language,
    set_current_output_language,
    set_current_ui_language,
)
from bowhead_core.report_chrome_text import chrome_text
from bowhead_core.sources_resolver import (
    TurnSourcesRegistry,
    number_duplicate_source_labels,
    source_label_language,
)

_DISPLAY_KEY = {"zh-CN": "display_zh", "en-US": "display_en", "de-DE": "display_de", "fr-FR": "display_fr"}
_OTHER_UI = {"zh-CN": "en-US", "en-US": "zh-CN", "de-DE": "zh-CN", "fr-FR": "en-US"}


@contextmanager
def _languages(*, answer: str, ui: str) -> Iterator[None]:
    out_token = set_current_output_language(answer)
    ui_token = set_current_ui_language(ui)
    try:
        yield
    finally:
        reset_current_ui_language(ui_token)
        reset_current_output_language(out_token)


@pytest.mark.parametrize("lang", ["zh-CN", "en-US", "de-DE", "fr-FR"])
def test_sandbox_labels_use_the_answer_language_for_prefix_and_suffix(lang: str) -> None:
    with _languages(answer=lang, ui=_OTHER_UI[lang]):
        assert source_label_language() == lang
        reg = TurnSourcesRegistry()
        reg.register_source("art_a", tool="python_sandbox", query="monthly change")
        reg.register_source("art_b", tool="python_sandbox", query="monthly change")
        entries = [dict(source) for source in reg.sources]
        number_duplicate_source_labels(entries, lang=source_label_language())

    prefix = TOOL_DISPLAY["python_sandbox"][_DISPLAY_KEY[lang]]
    assert [entry["label"] for entry in entries] == [
        f"{prefix}: monthly change{chrome_text('sources_run_ordinal', lang, n=1)}",
        f"{prefix}: monthly change{chrome_text('sources_run_ordinal', lang, n=2)}",
    ]


@pytest.mark.parametrize("lang", ["zh-CN", "en-US", "de-DE", "fr-FR"])
def test_run_ordinal_numbering_is_idempotent_in_every_language(lang: str) -> None:
    entries = [
        {"label": "Run Code: q", "tool": "python_sandbox", "artifact_id": "art_a"},
        {"label": "Run Code: q", "tool": "python_sandbox", "artifact_id": "art_b"},
    ]
    number_duplicate_source_labels(entries, lang=lang)
    first = [entry["label"] for entry in entries]
    number_duplicate_source_labels(entries, lang=lang)
    assert [entry["label"] for entry in entries] == first
    assert first[0] == "Run Code: q" + chrome_text("sources_run_ordinal", lang, n=1)
    # A later run of the same turn joins the numbered group instead of standing bare beside it.
    entries.append({"label": "Run Code: q", "tool": "python_sandbox", "artifact_id": "art_c"})
    number_duplicate_source_labels(entries, lang=lang)
    assert [entry["label"] for entry in entries] == ["Run Code: q" + chrome_text("sources_run_ordinal", lang, n=n) for n in (1, 2, 3)]


def test_persisted_label_prefix_is_not_rewritten() -> None:
    """History stays as stored: renumbering touches only the suffix it owns."""
    entries = [
        {"label": "Run Code: q（第 1 次运行）", "tool": "python_sandbox", "artifact_id": "art_a"},
        {"label": "Run Code: q（第 2 次运行）", "tool": "python_sandbox", "artifact_id": "art_b"},
    ]
    number_duplicate_source_labels(entries, lang="zh-CN")
    assert [entry["label"] for entry in entries] == ["Run Code: q（第 1 次运行）", "Run Code: q（第 2 次运行）"]


def test_without_an_answer_language_labels_use_english() -> None:
    with _languages(answer="", ui="zh-CN"):
        assert source_label_language() == "en-US"
        reg = TurnSourcesRegistry()
        reg.register_source("art_a", tool="bi_query", query="revenue")
    assert reg.sources[0]["label"] == f"{TOOL_DISPLAY['bi_query']['display_en']}: revenue"

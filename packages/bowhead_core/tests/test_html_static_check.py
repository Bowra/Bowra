"""Static HTML findings stay bounded, read-only and distinct from runtime checks."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bowhead_core.html_preview_policy import html_preview_policy
from bowhead_core.html_static_check import inspect_html, inspect_retained_html


def test_duplicate_section_table_ids_and_missing_contents_targets_are_reported():
    source = b'<a href="#missing">Next</a><section id="t1"><table id="t1"></table></section>'
    result = inspect_html(source)
    assert result["status"] == "issues_found"
    assert result["runtime_checked"] is False
    assert result["content_sha256"] == hashlib.sha256(source).hexdigest()
    assert [(item["code"], item["value"]) for item in result["findings"]] == [
        ("duplicate_id", "t1"),
        ("missing_fragment_target", "missing"),
    ]
    assert result["findings"][0]["first_tag"] == "section"
    assert result["findings"][0]["tag"] == "table"


def test_encoded_named_and_top_links_resolve_without_false_template_targets():
    source = """<a href="#caf%C3%A9">Cafe</a><section id="café"></section>
    <a href="about:srcdoc#legacy">Legacy</a><a name="legacy"></a>
    <a href="#">Top</a><a href="#TOP">Top</a><a href="#:~:text=hello">Text</a>
    <template><div id="café"></div><a href="#absent">Dormant</a></template>
    <script>const sample='<div id="café">';</script>""".encode()
    result = inspect_html(source)
    assert result["status"] == "no_static_issues_found"
    assert result["runtime_checked"] is False


def test_offline_dependency_diagnostics_do_not_echo_urls_or_credentials():
    result = inspect_html(
        b'<script src="https://private.invalid/x?token=secret"></script><img src="data:image/png;base64,AAAA"><link href="local.css" rel="stylesheet">'
    )
    assert result["total_findings"] == 2
    assert {item["code"] for item in result["findings"]} == {"external_dependency"}
    assert "private.invalid" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_findings_have_explicit_coverage_counts_and_bounded_details():
    source = ('<div id="' + "x" * 200 + '"></div>') * 31
    result = inspect_html(source.encode())
    assert result["total_findings"] == 30
    assert len(result["findings"]) == 20
    assert result["truncated"] is True
    assert len(result["findings"][0]["value"]) == 120


def test_unknown_encoding_does_not_claim_a_successful_check():
    assert inspect_html(b"\xff<html>") == {
        "status": "not_checked",
        "reason": "unsupported_encoding",
        "runtime_checked": False,
    }


def test_structure_budget_does_not_turn_a_partial_scan_into_success(monkeypatch):
    monkeypatch.setattr("bowhead_core.html_static_check._MAX_ELEMENTS", 2)
    assert inspect_html(b"<main><div></div><div></div></main>") == {
        "status": "not_checked",
        "reason": "structure_budget",
        "runtime_checked": False,
    }


async def test_existing_team_byte_limit_skips_reads_without_revoking_the_file():
    backend = SimpleNamespace(read_bounded=AsyncMock())
    token = html_preview_policy.set(10)
    try:
        result = await inspect_retained_html(SimpleNamespace(backend=backend), SimpleNamespace(mime="text/html", size=11))
    finally:
        html_preview_policy.reset(token)
    assert result["status"] == "not_checked"
    assert result["reason"] == "preview_byte_limit"
    backend.read_bounded.assert_not_awaited()


async def test_read_failure_is_an_explicit_unchecked_result_not_a_generation_failure():
    backend = SimpleNamespace(read_bounded=AsyncMock(side_effect=OSError("unavailable")))
    result = await inspect_retained_html(
        SimpleNamespace(backend=backend), SimpleNamespace(mime="text/html", size=1, path="fixture", id="art_fixture")
    )
    assert result == {"status": "not_checked", "reason": "inspection_unavailable", "runtime_checked": False}


async def test_non_html_does_not_add_work_or_inspection_claims():
    backend = SimpleNamespace(read_bounded=AsyncMock())
    assert await inspect_retained_html(SimpleNamespace(backend=backend), SimpleNamespace(mime="application/pdf")) is None
    backend.read_bounded.assert_not_awaited()

"""Exported reports link Google cards to the file itself, never the preview URL naming the member."""

from __future__ import annotations

from bowhead_core.export_ir import _adapt_interactive, _AdapterContext
from bowhead_core.export_snapshot import _sanitize_section_payload
from bowhead_core.source_access_projection import StaticSafeSourceMap

_CARD = {
    "url": "https://docs.google.com/document/d/doc-1/preview?authuser=member%40acme.example",
    "title": "Q3 report",
    "height": 560,
    "provider": "google_workspace",
    "file_kind": "document",
    "file_id": "doc-1",
    "open_url": "https://docs.google.com/document/d/doc-1",
    "snapshot": {"format": "markdown", "content": "# Summary", "truncated": False},
}


def _exported_url(payload: dict) -> str | None:
    frozen = _sanitize_section_payload("external_iframe", payload, section_counter=[0], source_map=StaticSafeSourceMap())
    context = _AdapterContext(
        top_section_id="sec_1",
        top_section_rev=1,
        kind="external_iframe",
        payload=frozen,
        source_refs=(),
        block_key="b_1",
        order=0,
        section_path=(0,),
        surface_placement="body",
        locale="en-US",
        theme_hash="t",
        renderer_version="r",
        asset_content_hashes={},
        resolved_chart_rows={},
        renderer_contract_version=1,
        frozen_chart_presentation=None,
    )
    return _adapt_interactive(context).content["safe_url"]


def test_google_card_exports_the_file_link_without_the_member_email() -> None:
    url = _exported_url(_CARD)
    assert url == "https://docs.google.com/document/d/doc-1"
    assert "authuser" not in url


def test_plain_external_iframe_still_exports_its_url() -> None:
    assert _exported_url({"url": "https://example.com/dash", "title": "Dash"}) == "https://example.com/dash"

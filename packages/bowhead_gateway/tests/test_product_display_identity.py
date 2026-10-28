"""Fixed edition identity is visible in public HTML and localized channel cards."""

import json

import pytest
from bowhead_gateway.feishu import renderer
from bowhead_gateway.im.error_messages import im_interface_message
from bowhead_gateway.routes import slack, teams
from bowhead_gateway.static_localized import _rewrite_html_for_base_path, public_document_title

from bowhead_gateway import edition_extensions


@pytest.fixture(params=[("community", "Bowra"), ("business", "Bowhead")])
def packaged_product(request, tmp_path, monkeypatch):
    edition, name = request.param
    manifest = tmp_path / "build_manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "edition": edition}), encoding="utf-8")
    monkeypatch.setattr(edition_extensions, "BUILD_MANIFEST_PATH", manifest)
    return name


@pytest.mark.parametrize("base_path", ["", "/bowhead"])
def test_public_title_uses_packaged_name_and_preserves_custom_names(packaged_product, base_path):
    expected = f"{packaged_product} — Verified Intelligence"
    assert public_document_title(None) == expected
    assert public_document_title({"brand": {"applicationName": packaged_product}}) == expected
    assert public_document_title({"brand": {"applicationName": "Bowhead Customer"}}) == "Bowhead Customer"
    html = _rewrite_html_for_base_path("<html><head></head><body></body></html>", base_path)
    assert f"applyMetadata({json.dumps(expected)}" in html


@pytest.mark.parametrize("locale", ["en-US", "zh-CN", "de-DE", "fr-FR"])
def test_channel_cards_use_packaged_name_without_rewriting_content(packaged_product, locale):
    authored = "Bowhead Customer"
    title = im_interface_message("answer_title", locale)
    assert packaged_product in title
    for card in (
        renderer.answer_card(authored, locale=locale),
        slack._turn_event_card("final_answer", {"text": authored}, locale, locale),
        teams._card(*teams._turn_event_title_and_body("error", locale, locale)),
    ):
        assert packaged_product in json.dumps(card, ensure_ascii=False)
    assert authored in json.dumps(renderer.answer_card(authored, locale=locale))
    assert authored in json.dumps(slack._turn_event_card("final_answer", {"text": authored}, locale, locale))

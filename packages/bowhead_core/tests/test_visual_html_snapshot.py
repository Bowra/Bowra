"""Visual self-check slice B: HTML and SVG captured by the sandbox document worker.

Design: docs/sprints/visual-self-check-2026-11-16/tech-design.md §6 (FR5, FR13). The executor
and the browser are replaced by fakes here; the worker capture has a local end-to-end check
in the sandbox image (see the rollout register).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, ImageDraw

from bowhead_core import html_snapshot as hs
from bowhead_core import visual_self_check as vsc
from bowhead_core import visual_view as vv

TURN = "t_html"


@pytest.fixture(autouse=True)
def fresh_state():
    vsc._RUNS.clear()
    hs._semaphores.clear()
    yield
    vsc._RUNS.clear()


def _page_png(height: int, *, blank: bool = False) -> bytes:
    image = Image.new("RGB", (1280, height), "white")
    if not blank:
        draw = ImageDraw.Draw(image)
        for top in range(40, height - 40, 300):
            draw.rectangle((80, top, 900, top + 120), fill=(40, 60, 120))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_a_long_page_is_one_full_screen_and_a_sheet() -> None:
    view = vv.render_screens_view(_page_png(2000), kind="html")
    assert view["status"] == "shown" and view["pages"] == 3
    assert [image["label"] for image in view["images"]] == ["Screen 1", "screens 1-3"]
    # Page numbers let a later view_file bring back each page once (visual_self_check.earlier_views).
    assert [(image.get("page"), image.get("pages")) for image in view["images"]] == [(1, None), (None, [1, 2, 3])]
    # Cut at a white band, sent at its own height: rows through a solid dark block are not blank, although they have
    # few colour changes (a cut inside a purple divider read as an overflowing strip, an observed deployment).
    assert view["images"][0]["px"] == [1280, 780]
    assert view["shown"].startswith("screen 1 at 1280x780 px, then screens 1-3 in 1 sheet")
    assert view["page_px"] == [400, 244]


def test_short_page_full_detail_and_blank_capture() -> None:
    single = vv.render_screens_view(_page_png(600), kind="html")
    assert single["shown"] == "screen 1, 1280x600 px" and len(single["images"]) == 1
    full = vv.render_screens_view(_page_png(4000), kind="html", pages=[1, 4], detail="full")
    assert [image["label"] for image in full["images"]] == ["Screen 2", "Screen 5"]
    assert [image["page"] for image in full["images"]] == [2, 5]
    assert vv.render_screens_view(_page_png(1600, blank=True), kind="html") == {"status": "not_completed", "reason": "blank_render", "pages": 3}
    assert vv.render_screens_view(_page_png(1600, blank=True), kind="html", text_present=False)["status"] == "shown"


def _content_png(blocks: list[tuple[int, int]], height: int) -> bytes:
    """A white page whose content blocks are vertical stripes, so every row through them has many colour changes."""
    image = Image.new("RGB", (1280, height), "white")
    draw = ImageDraw.Draw(image)
    for top, bottom in blocks:
        for left in range(80, 1200, 10):
            draw.rectangle((left, top, left + 4, bottom), fill=(40, 60, 120))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_screens_end_at_blank_bands_instead_of_every_800_px() -> None:
    # A chart from 600 to 1000 px: an 800 px cut ran through it and models fixed a top that only looked clipped
    # (findings spike §6).
    png = _content_png([(100, 300), (600, 1000), (1100, 1500)], 1700)
    assert [cut[:2] for cut in vv._screen_cuts(Image.open(io.BytesIO(png)).convert("RGB"))] == [(0, 499), (499, 1050), (1050, 1700)]
    view = vv.render_screens_view(png, kind="html", pages=[0, 1], detail="full")
    assert view["pages"] == 3 and view["shown"] == "screens 1-2, 1280 px wide and 499, 551 px tall"
    # Each screen at its own height: grey filled up to 800 px read as page whitespace (an observed deployment).
    assert [image["px"] for image in view["images"]] == [[1280, 499], [1280, 551]]
    first = Image.open(io.BytesIO(base64.b64decode(view["images"][0]["base64"])))
    assert first.size == (1280, 499)
    sheet = vv.render_screens_view(png, kind="html")
    assert sheet["shown"] == "screen 1 at 1280x499 px, then screens 1-3 in 1 sheet, each screen 400 px wide"


def test_a_slide_keeps_its_footer_on_its_screen() -> None:
    # Cut in the tallest band, the empty lower half of a slide, the footer went to the next screen and models moved
    # footers and reflowed the deck to fit the screens (an observed deployment, findings spike §6).
    png = _content_png([(100, 300), (700, 720), (760, 1500)], 1600)
    assert vv._screen_cuts(Image.open(io.BytesIO(png)).convert("RGB"))[0] == (0, 740, False)


def test_a_page_without_a_blank_band_is_cut_at_800_px_and_says_so() -> None:
    view = vv.render_screens_view(_content_png([(0, 1999)], 2000), kind="html")
    assert view["pages"] == 3
    assert view["shown"].endswith(
        "; screen 1 ends inside content that continues on screen 2; screen 2 ends inside content that continues on screen 3"
    )


def test_svg_capture_is_one_image() -> None:
    view = vv.render_screens_view(_page_png(500), kind="svg")
    assert view["status"] == "shown" and view["shown"].startswith("the SVG at")
    assert vv.render_screens_view(_page_png(500, blank=True), kind="svg")["reason"] == "blank_render"


def test_a_sparse_page_is_not_blank() -> None:
    # 116 acceptance prep: one heading on white was called blank by the thumbnail share check.
    image = Image.new("RGB", (1280, 800), "white")
    ImageDraw.Draw(image).text((20, 20), "Egress", fill=(0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    assert vv.render_screens_view(buffer.getvalue(), kind="html")["status"] == "shown"


class _Backend:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_bounded(self, path: str, *, limit: int) -> bytes:
        return self.files[path][:limit]


def _artifact(backend: _Backend, artifact_id: str, name: str, content: str) -> Any:
    data = content.encode()
    backend.files[artifact_id] = data
    return SimpleNamespace(
        id=artifact_id, filename=name, size=len(data), path=artifact_id, lineage_root=artifact_id, created_by_turn=TURN, mime="text/html"
    )


def _ctx(backend: _Backend, call_id: str) -> Any:
    return SimpleNamespace(
        turn_id=TURN, artifact_store=SimpleNamespace(backend=backend), tool_step=SimpleNamespace(call_id=call_id, parent_agent_id=None)
    )


def _fake_capture(texts: list[str], **facts: Any):
    calls: list[str] = []

    async def capture(kind: str, content: bytes, **_kwargs: Any) -> hs.HtmlSnapshot:
        calls.append(kind)
        return hs.HtmlSnapshot(
            png=_page_png(1700),
            text=texts[min(len(calls), len(texts)) - 1],
            scroll_width=1280,
            scroll_height=1700,
            issues=tuple(facts.get("issues", ())),
            external_resources=facts.get("external", False),
            layout=facts.get("layout"),
        )

    return capture, calls


def _layout() -> dict[str, Any]:
    """The measured layout of ``_page_png(1700)``: two blocks, one chart, one finding."""
    return {
        "version": 1,
        "width": 1280,
        "height": 1700,
        "blocks": [{"y": 0, "h": 800, "label": "Overview"}, {"y": 800, "h": 900, "label": "Detail"}],
        "guards": [[40, 160], [340, 460], [640, 760], [940, 1060], [1240, 1360], [1540, 1660]],
        "elements": [{"kind": "chart", "x": 80, "y": 940, "w": 820, "h": 120, "label": ""}],
        "findings": [{"kind": "text_overlap", "selector": "", "text": "Q3", "box": {"x": 100, "y": 950, "w": 30, "h": 14}, "other_text": "Q4"}],
    }


async def test_a_saved_page_with_its_layout_names_screens_findings_and_versions(monkeypatch) -> None:
    capture, _ = _fake_capture(["Total 163.42", "Total 163.43"], layout=_layout())
    monkeypatch.setattr(hs, "capture", capture)
    run = vsc.begin_run(TURN, "lead", accepts_images=True)
    backend = _Backend()
    await vsc.attach_visual_view({}, _ctx(backend, "call_1"), _artifact(backend, "a1", "report.html", "<p>v1</p>"), kind="html")
    content = vsc.render_message(run.pending).content
    assert "  Screen 1: page y 0-800 of 1,700 px; block b1 of 2 ('Overview', page y 0-800)." in content
    assert "  Screens: 1 = page y 0-800 (b1), 2 = page y 800-1,240 (b2), 3 = page y 1,240-1,700 (b2)." in content
    assert "  Measured layout of report.html: At page y 950 the text 'Q3' overlaps the text 'Q4'." in content
    assert "version" not in content.split("\n")[1]  # the first version of a file has no version line
    run.pending.clear()
    await vsc.attach_visual_view({}, _ctx(backend, "call_2"), _artifact(backend, "a2", "report.html", "<p>v2</p>"), kind="html")
    assert "  This is version 2 of report.html rendered in this run." in vsc.render_message(run.pending).content


async def test_view_file_shows_an_element_or_a_region_of_an_html_page(monkeypatch) -> None:
    from bowhead_core import artifact_presentation

    capture, calls = _fake_capture(["Total 163.42"], layout=_layout())
    monkeypatch.setattr(hs, "capture", capture)
    backend = _Backend()
    page = _artifact(backend, "a1", "report.html", "<p>x</p>")

    async def available(_ctx, artifact_id: str, *, produced: bool = False):
        return page

    monkeypatch.setattr(artifact_presentation, "available_artifact", available)
    run = vsc.begin_run(TURN, "lead", accepts_images=True)
    element = await vsc.view_file({"artifact_id": "a1", "element": "e1"}, _ctx(backend, "call_e"))
    assert element["visual_view"]["shown"] == "element e1 (chart, page y 940-1,060 of 1,700 px), 852x152 px"
    message = vsc.render_message(run.pending).content
    assert "- report.html (Tool call call_e): element e1 (chart, page y 940-1,060 of 1,700 px), 852x152 px (image 1)." in message
    assert "Text smaller than about 10 px" not in message  # a crop is at capture resolution
    region = await vsc.view_file({"artifact_id": "a1", "region": [0, 0, 640, 400]}, _ctx(backend, "call_r"))
    assert region["visual_view"]["shown"] == "page region x 0-640, y 0-400 of 1,700 px, 640x400 px" and calls == ["html"]
    missing = await vsc.view_file({"artifact_id": "a1", "element": "e5"}, _ctx(backend, "call_m"))
    assert missing["visual_view"]["reason"] == "view_target_invalid"
    assert missing["visual_view"]["message"].startswith("'e5' is not a ref of this version of the file; its refs are b1-b2, e1")
    for arguments in ({"element": "e1", "pages": [1]}, {"region": [0, 0, 10]}, {"element": ""}):
        refused = await vsc.view_file({"artifact_id": "a1", **arguments}, _ctx(backend, "call_x"))
        assert refused["ok"] is False and refused["error"] == "invalid_arguments"


@pytest.mark.parametrize("name", ["Bowhead", "Bowra"])
async def test_saved_html_is_shown_with_page_facts(monkeypatch, name: str) -> None:
    capture, calls = _fake_capture(["全年销售额 0.00 亿元\n销售额 163.42 亿元"], issues=["runtime"], external=True)
    monkeypatch.setattr(hs, "capture", capture)
    monkeypatch.setattr(vsc, "product_name", lambda: name)
    run = vsc.begin_run(TURN, "lead", accepts_images=True)
    backend = _Backend()
    output: dict[str, Any] = {}
    await vsc.attach_visual_view(output, _ctx(backend, "call_1"), _artifact(backend, "a1", "report.html", "<p>x</p>"), kind="html")
    view = output["visual_view"]
    assert view["status"] == "shown" and view["kind"] == "html" and view["pages"] == 3
    assert view["scroll_height"] == 1700 and view["runtime_issues"] == ["runtime"] and view["external_resources_removed"] is True
    message = vsc.render_message(run.pending)
    assert f"The user's preview in {name} removes external scripts, stylesheets, fonts and images" in message.content
    assert "must work" not in message.content  # a fact, not an instruction to merge the user's files
    assert "The page reported script errors." in message.content and len(message.images) == 2
    # The same bytes again are not captured again.
    again: dict[str, Any] = {}
    await vsc.attach_visual_view(again, _ctx(backend, "call_2"), _artifact(backend, "a2", "report.html", "<p>x</p>"), kind="html")
    assert again["visual_view"]["reason"] == "unchanged" and calls == ["html"]


async def test_a_declared_fix_of_an_html_page_compares_its_visible_text(monkeypatch) -> None:
    capture, _ = _fake_capture(["全年销售额 0.00 亿元", "全年销售额 163.42 亿元"])
    monkeypatch.setattr(hs, "capture", capture)
    vsc.begin_run(TURN, "lead", accepts_images=True)
    backend = _Backend()
    await vsc.attach_visual_view({}, _ctx(backend, "call_1"), _artifact(backend, "a1", "report.html", "<p>v1</p>"), kind="html")
    fixed: dict[str, Any] = {}
    await vsc.attach_visual_view(
        fixed, _ctx(backend, "call_2"), _artifact(backend, "a2", "report.html", "<p>v2</p>"), kind="html", presentation_fix=True
    )
    assert fixed["presentation_check"] == {"status": "content_changed"}
    assert fixed["content_changes"]["added_numbers"] == ["163.42"] and fixed["content_changes"]["removed_numbers"] == ["0.00"]


async def test_capture_failures_are_not_completed(monkeypatch) -> None:
    async def busy(kind: str, content: bytes, **_kwargs: Any) -> hs.HtmlSnapshot:
        raise hs.HtmlSnapshotUnavailable("renderer_busy")

    monkeypatch.setattr(hs, "capture", busy)
    vsc.begin_run(TURN, "lead", accepts_images=True)
    backend = _Backend()
    output: dict[str, Any] = {}
    await vsc.attach_visual_view(output, _ctx(backend, "call_1"), _artifact(backend, "a1", "game.html", "<canvas></canvas>"), kind="html")
    assert output["visual_view"] == {"status": "not_completed", "kind": "html", "reason": "renderer_busy"}


async def test_view_file_shows_chosen_screens_of_an_html_file(monkeypatch) -> None:
    from bowhead_core import artifact_presentation

    capture, _ = _fake_capture(["Total 163.42"])
    monkeypatch.setattr(hs, "capture", capture)
    backend = _Backend()
    page = _artifact(backend, "a1", "report.html", "<p>x</p>")

    async def available(_ctx, artifact_id: str, *, produced: bool = False):
        return page

    monkeypatch.setattr(artifact_presentation, "available_artifact", available)
    run = vsc.begin_run(TURN, "lead", accepts_images=True)
    result = await vsc.view_file({"artifact_id": "a1", "pages": [2], "detail": "full"}, _ctx(backend, "call_v"))
    assert result["visual_view"]["shown"] == "screen 2, 1280x670 px" and len(run.pending) == 1
    assert len(run.baselines) == 1  # saved in this Turn: the looked-at text is the version last seen


async def test_view_file_reuses_the_capture_of_unchanged_content(monkeypatch) -> None:
    from bowhead_core import artifact_presentation

    capture, calls = _fake_capture(["Total 163.42"])
    monkeypatch.setattr(hs, "capture", capture)
    backend = _Backend()
    files = {"a1": _artifact(backend, "a1", "report.html", "<p>v1</p>")}

    async def available(_ctx, artifact_id: str, *, produced: bool = False):
        return files[artifact_id]

    monkeypatch.setattr(artifact_presentation, "available_artifact", available)
    run = vsc.begin_run(TURN, "lead", accepts_images=True)
    await vsc.attach_visual_view({}, _ctx(backend, "call_1"), files["a1"], kind="html")
    viewed = await vsc.view_file({"artifact_id": "a1", "pages": [3], "detail": "full"}, _ctx(backend, "call_v"))
    assert viewed["visual_view"]["shown"] == "screen 3, 1280x250 px" and calls == ["html"]
    files["a2"] = _artifact(backend, "a2", "report.html", "<p>v2</p>")
    await vsc.view_file({"artifact_id": "a2"}, _ctx(backend, "call_w"))
    assert calls == ["html", "html"] and len(run.pending) == 3  # changed content is captured again


_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><style>text{{font:12px sans-serif}}</style>'
    '<text x="{x}" y="20">营业额<tspan>({unit})</tspan></text><text x="10" y="40">163.42</text></svg>'
)


def test_svg_content_snapshot_reads_text_elements() -> None:
    svg = (
        '<!DOCTYPE svg [<!ENTITY x "expanded">]><svg xmlns="http://www.w3.org/2000/svg"><title>t</title>'
        '<text>  A <tspan>B</tspan></text><foreignObject><div xmlns="http://www.w3.org/1999/xhtml"><p>C</p></div></foreignObject>'
        "<text>&x;</text></svg>"
    )
    assert vv.svg_content_snapshot(svg.encode())["texts"] == [["A B", "C", "&x;"]]  # entities are never expanded
    assert vv.svg_content_snapshot(b"<svg><text>open") is None


async def test_a_declared_fix_of_an_svg_compares_its_text(monkeypatch) -> None:
    capture, calls = _fake_capture([""])
    monkeypatch.setattr(hs, "capture", capture)
    run = vsc.begin_run(TURN, "lead", accepts_images=True)
    backend = _Backend()
    await vsc.attach_visual_view({}, _ctx(backend, "call_1"), _artifact(backend, "a1", "chart.svg", _SVG.format(x=10, unit="亿元")), kind="svg")
    moved: dict[str, Any] = {}
    await vsc.attach_visual_view(
        moved, _ctx(backend, "call_2"), _artifact(backend, "a2", "chart.svg", _SVG.format(x=30, unit="亿元")), kind="svg", presentation_fix=True
    )
    assert moved["presentation_check"] == {"status": "no_content_change"}
    renamed: dict[str, Any] = {}
    await vsc.attach_visual_view(
        renamed, _ctx(backend, "call_3"), _artifact(backend, "a3", "chart.svg", _SVG.format(x=30, unit="亿")), kind="svg", presentation_fix=True
    )
    assert renamed["presentation_check"] == {"status": "content_changed"}
    assert renamed["content_changes"]["removed_text"] == ["page 1: 营业额(亿元)"] and calls == ["svg"] * 3
    assert len(run.pending) == 3
    # Through the parse worker an SVG capture is one image, not screens of an HTML page.
    assert run.pending[0].view["shown"].startswith("the SVG at ") and len(run.pending[0].view["images"]) == 1


# ------------------------------------------------------------------ sandbox adapter

TRANSFORM = b"globalThis.bowheadHtmlPreview = {};"


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    transform = tmp_path / "runtime" / "dist" / hs.TRANSFORM_NAME
    transform.parent.mkdir(parents=True)
    transform.write_bytes(TRANSFORM)
    monkeypatch.setenv("BOWHEAD_EXPORT_FRONTEND_DIR", str(tmp_path / "runtime"))
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_URL", "http://executor.invalid")
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_TOKEN", "token")
    hs._transform_cache.clear()
    calls: list[tuple[str, bytes, bytes]] = []
    behaviour: dict[str, Any] = {"readiness": {"document_slots": 2}}

    async def capabilities(self) -> dict:
        if "readiness_error" in behaviour:
            raise behaviour["readiness_error"]
        return behaviour["readiness"]

    async def view_html(self, kind: str, source: bytes, transform: bytes, *, readiness: dict | None = None) -> Any:
        from bowhead_core.documents.authoring_executor import HtmlView

        assert readiness is behaviour["readiness"]  # read once, before queueing
        calls.append((kind, source, transform))
        if "raise" in behaviour:
            raise behaviour["raise"]
        if "sleep" in behaviour:
            await asyncio.sleep(behaviour["sleep"])
        return HtmlView(_page_png(900), "全年销售额 163.42 亿元", 1280, 900, ("blocked",), True)

    from bowhead_core.documents.authoring_executor import AuthoringExecutor

    monkeypatch.setattr(AuthoringExecutor, "capabilities", capabilities)
    monkeypatch.setattr(AuthoringExecutor, "view_html", view_html)
    return SimpleNamespace(calls=calls, behaviour=behaviour, transform=transform)


async def test_capture_sends_the_file_and_the_preview_transform_to_the_sandbox(sandbox) -> None:
    snapshot = await hs.capture("html", "<p>销售额</p>".encode())
    assert sandbox.calls == [("html", "<p>销售额</p>".encode(), TRANSFORM)]
    assert snapshot.text == "全年销售额 163.42 亿元" and snapshot.issues == ("blocked",) and snapshot.external_resources
    assert snapshot.scroll_height == 900 and snapshot.png.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("document_html_time_limit", "time_limit"),
        ("document_html_resource_limited", "too_large"),
        ("document_html_render_failed", "render_failed"),
        ("document_authoring_output_invalid", "render_failed"),
        ("document_runtime_unavailable", "renderer_unavailable"),
    ],
)
async def test_capture_maps_sandbox_failures(sandbox, code, reason) -> None:
    from bowhead_core.documents.package_io import OfficePackageError

    sandbox.behaviour["raise"] = OfficePackageError(code)
    with pytest.raises(hs.HtmlSnapshotUnavailable) as caught:
        await hs.capture("html", b"<p>x</p>")
    assert caught.value.reason == reason


async def test_capture_limits_size_runtime_time_and_concurrency(sandbox, monkeypatch) -> None:
    from bowhead_core.documents.authoring_protocol import MAX_HTML_VIEW_SOURCE

    with pytest.raises(hs.HtmlSnapshotUnavailable, match="too_large"):
        await hs.capture("html", b"x" * (MAX_HTML_VIEW_SOURCE + 1))
    sandbox.transform.unlink()
    with pytest.raises(hs.HtmlSnapshotUnavailable, match="renderer_unavailable"):
        await hs.capture("html", b"<p>x</p>")
    sandbox.transform.write_bytes(TRANSFORM)
    monkeypatch.delenv("BOWHEAD_SANDBOX_EXECUTOR_URL")
    with pytest.raises(hs.HtmlSnapshotUnavailable, match="renderer_unavailable"):
        await hs.capture("html", b"<p>x</p>")
    assert sandbox.calls == []
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_URL", "http://executor.invalid")
    monkeypatch.setattr(hs, "WAIT_SECONDS", 0.05)
    sandbox.behaviour["sleep"] = 1
    with pytest.raises(hs.HtmlSnapshotUnavailable, match="time_limit"):
        await hs.capture("html", b"<p>x</p>")
    monkeypatch.setattr(hs, "QUEUE_WAIT_SECONDS", 0.05)
    semaphore = hs._semaphore(2)
    for _ in range(2):
        await semaphore.acquire()
    with pytest.raises(hs.HtmlSnapshotUnavailable, match="renderer_busy"):
        await hs.capture("html", b"<p>x</p>")
    for _ in range(2):
        semaphore.release()


@pytest.mark.parametrize(("readiness", "expected"), [({"document_slots": 6}, 6), ({}, 2), ({"document_slots": 0}, 2), ({"document_slots": "6"}, 2)])
async def test_captures_at_once_follow_the_executor_slots(sandbox, monkeypatch, readiness, expected) -> None:
    # The executor sizes its document run slots to the host (2026-11-20); an older one says nothing.
    sandbox.behaviour.update(readiness=readiness, sleep=0.2)
    monkeypatch.setattr(hs, "QUEUE_WAIT_SECONDS", 0.05)
    results = await asyncio.gather(*(hs.capture("html", b"<p>x</p>") for _ in range(expected + 1)), return_exceptions=True)
    busy = [result for result in results if isinstance(result, hs.HtmlSnapshotUnavailable)]
    assert len(sandbox.calls) == expected and [error.reason for error in busy] == ["renderer_busy"]


async def test_an_unreachable_executor_is_renderer_unavailable_before_queueing(sandbox) -> None:
    from bowhead_core.documents.package_io import OfficePackageError

    sandbox.behaviour["readiness_error"] = OfficePackageError("document_runtime_unavailable")
    with pytest.raises(hs.HtmlSnapshotUnavailable, match="renderer_unavailable"):
        await hs.capture("html", b"<p>x</p>")
    assert sandbox.calls == [] and hs._semaphores == {}


# ------------------------------------------------------------------ document worker contract


def test_html_view_control_binds_the_file_the_transform_and_the_kind() -> None:
    from bowhead_core.documents.authoring_protocol import control, digest
    from bowhead_core.documents.package_io import OfficePackageError

    page = b"<p>x</p>"
    encoded = json.loads(control("html_view", (page, TRANSFORM, b"html"), parent_sha256=digest(page)))
    assert encoded["operation"] == "html_view" and encoded["input_sha256"] == [digest(page), digest(TRANSFORM), digest(b"html")]
    for inputs, parent in [
        ((page, TRANSFORM, b"pdf"), digest(page)),
        ((page, b"", b"html"), digest(page)),
        ((page, TRANSFORM, b"svg"), digest(b"other")),
        ((page, TRANSFORM), digest(page)),
    ]:
        with pytest.raises(OfficePackageError):
            control("html_view", inputs, parent_sha256=parent)


async def _executor_reading(tmp_path: Path, write) -> Any:
    from bowhead_core.documents.authoring_executor import AuthoringExecutor
    from bowhead_core.documents.authoring_protocol import PROFILE

    tmp_path.mkdir(parents=True)
    executor = AuthoringExecutor(root=tmp_path, url="http://executor.invalid", token="token")
    seen: dict[str, Any] = {}

    async def capabilities() -> dict:
        return {"profile": PROFILE, "image_id": "sha256:" + "c" * 64, "html_view_contract": "html-view-v1"}

    async def dispatch(run_id, inputs, encoded, *, expected_image_id, profile):
        directory = tmp_path / "run"
        (directory / "output").mkdir(parents=True)
        seen["names"] = [item.name for item in inputs]
        write(directory / "output", encoded, inputs)
        return directory, {}

    executor.capabilities = capabilities
    executor._dispatch = dispatch
    return executor, seen


async def test_the_executor_accepts_only_a_result_bound_to_this_capture(tmp_path) -> None:
    from bowhead_core.documents.authoring_protocol import PROFILE, digest
    from bowhead_core.documents.package_io import OfficePackageError

    png, text = _page_png(900), b"Total 163.42"

    def good(output: Path, encoded: bytes, inputs, **changes: Any) -> None:
        (output / "snapshot.png").write_bytes(png)
        (output / "text.txt").write_bytes(text)
        record = {
            "profile": PROFILE,
            "kind": "html_view",
            "control_sha256": digest(encoded),
            "source_sha256": digest(inputs[0].content),
            "png_sha256": digest(png),
            "text_sha256": digest(text),
            "scroll_width": 1280,
            "scroll_height": 900,
            "issues": ["runtime"],
            "external_resources": False,
            **changes,
        }
        (output / "result.json").write_text(json.dumps(record))

    executor, seen = await _executor_reading(tmp_path / "a", good)
    view = await executor.view_html("html", b"<p>x</p>", TRANSFORM)
    assert seen["names"] == ["page.html", "html-preview-transform.js", "kind.txt"]
    assert view.png == png and view.text == "Total 163.42" and view.issues == ("runtime",)
    for index, changes in enumerate([{"png_sha256": digest(b"other")}, {"issues": ["os"]}, {"source_sha256": digest(b"other")}]):
        executor, _ = await _executor_reading(
            tmp_path / f"b{index}", lambda output, encoded, inputs, changes=changes: good(output, encoded, inputs, **changes)
        )
        with pytest.raises(OfficePackageError, match="document_authoring_output_invalid"):
            await executor.view_html("html", b"<p>x</p>", TRANSFORM)

    def failed(output: Path, encoded: bytes, inputs) -> None:
        record = {
            "profile": PROFILE,
            "kind": "authoring_error",
            "control_sha256": digest(encoded),
            "source_sha256": digest(inputs[0].content),
            "parent_sha256": digest(inputs[0].content),
            "error_code": "document_html_time_limit",
        }
        (output / "result.json").write_text(json.dumps(record))

    executor, _ = await _executor_reading(tmp_path / "c", failed)
    with pytest.raises(OfficePackageError, match="document_html_time_limit"):
        await executor.view_html("svg", b"<svg/>", TRANSFORM)

    # A sandbox image with the page layout adds layout.json, bound by its digest (visual-geometry tech design §4).
    layout = json.dumps({"version": 1, "width": 1280, "height": 900, "blocks": [], "guards": [], "elements": [], "findings": []}).encode()

    def with_layout(output: Path, encoded: bytes, inputs, *, written: bytes = layout, **changes: Any) -> None:
        (output / "layout.json").write_bytes(written)
        good(output, encoded, inputs, layout_sha256=digest(layout), **changes)

    executor, _ = await _executor_reading(tmp_path / "d", with_layout)
    assert (await executor.view_html("html", b"<p>x</p>", TRANSFORM)).layout["height"] == 900
    # A layout that does not match its digest or its shape is dropped; the capture stands.
    for index, written in enumerate([layout.replace(b"900", b"901"), b"[]"]):
        executor, _ = await _executor_reading(
            tmp_path / f"e{index}", lambda output, encoded, inputs, written=written: with_layout(output, encoded, inputs, written=written)
        )
        view = await executor.view_html("html", b"<p>x</p>", TRANSFORM)
        assert view.layout is None and view.png == png
    executor, _ = await _executor_reading(tmp_path / "f", lambda output, encoded, inputs: good(output, encoded, inputs, layout_sha256=7))
    with pytest.raises(OfficePackageError, match="document_authoring_output_invalid"):
        await executor.view_html("html", b"<p>x</p>", TRANSFORM)


def test_the_worker_deadline_writes_its_result_and_ends_the_process(monkeypatch, tmp_path) -> None:
    import threading

    from bowhead_core.documents import html_view

    exited = threading.Event()
    written: list[str] = []
    monkeypatch.setattr(html_view, "TOTAL_SECONDS", 0.05)
    monkeypatch.setattr(html_view.os, "_exit", lambda code: exited.set())

    def stuck(*_args: Any) -> dict[str, Any]:
        assert exited.wait(5)
        return {}

    monkeypatch.setattr(html_view, "_capture", stuck)
    html_view.render_html_view("html", b"<p>x</p>", TRANSFORM, tmp_path, on_deadline=lambda: written.append("time_limit"))
    assert written == ["time_limit"]
    # A capture that finishes first leaves no deadline behind.
    exited.clear()
    monkeypatch.setattr(html_view, "_capture", lambda *_args: {})
    monkeypatch.setattr(html_view, "TOTAL_SECONDS", 0.2)
    html_view.render_html_view("svg", b"<svg/>", TRANSFORM, tmp_path, on_deadline=lambda: written.append("late"))
    assert not exited.wait(0.4) and written == ["time_limit"]

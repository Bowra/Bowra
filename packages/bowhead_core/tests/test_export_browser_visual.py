"""Frontend Playwright visual backend stays fenced and snapshot-only."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.chart_style import PRESET_RELEASES, chart_style_policy_etag
from bowhead_core.chart_style_export import (
    CHART_EXPORT_SURFACE_CONTRACT_VERSION,
    build_chart_style_export_theme_v2,
    freeze_chart_presentation_v2,
)
from bowhead_core.chart_style_materializer import (
    DARK_STANDARD_SURFACE,
    actual_surface_projection,
)
from bowhead_core.chart_style_registry_generated import (
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
)
from bowhead_core.chart_style_v2 import (
    built_in_style_projection,
    chart_style_policy_etag_v2,
)
from bowhead_core.export_browser_visual import (
    BrowserVisualBackendError,
    PlaywrightVisualRenderer,
)
from bowhead_core.export_ir import (
    ExportIRBlock,
    ExportIRSource,
)
from bowhead_core.export_visual_identity import EXPORT_VISUAL_RENDERER_VERSION
from bowhead_core.print_theme import PRINT_PAPER, current_print_theme_snapshot
from PIL import Image

from bowhead_core import export_browser_visual as browser_visual_module

_REPO_ROOT = Path(__file__).parents[3]


def _theme() -> dict:
    preset = json.loads(
        (_REPO_ROOT / "config/chart_style/presets/bowhead-editorial.json").read_text(
            encoding="utf-8",
        )
    )
    return {
        "kind": "chart-style",
        "schemaVersion": 1,
        "mode": "light",
        "preset": preset,
        "presetChecksum": PRESET_RELEASES["bowhead-editorial"][1],
        "selectionSource": "product",
        "accessibility": {"contrast": "standard", "colorVision": "standard"},
        "teamPolicyEtag": chart_style_policy_etag(None),
        "resolverPolicyVersion": "1",
        "contextRecipeVersion": "1",
    }


def _block(*, rows: list[dict] | None = None) -> ExportIRBlock:
    spec: dict = {
        "kind": "bar",
        "title": "Revenue",
        "columns": [],
        "row_count": 1,
        "data_artifact_id": "art_rows",
    }
    if rows is not None:
        spec["rows"] = rows
    return ExportIRBlock(
        block_key="sec_chart",
        block_kind="visual",
        order=0,
        source=ExportIRSource(
            section_id="sec_chart",
            section_rev=1,
            section_kind="chart",
            section_path=(0,),
            surface_placement="report_body",
        ),
        source_refs=(),
        content={
            "payload": {"spec": spec},
            "artifact_content_hashes": {
                "art_rows": f"sha256:{'b' * 64}",
            },
        },
        semantic_tokens=("Revenue",),
        semantic_content_sha256=f"sha256:{'c' * 64}",
        typed_data_sha256=f"sha256:{'d' * 64}",
        visual_input_sha256=f"sha256:{'a' * 64}",
        warnings=(),
    )


def _theme_v2() -> dict:
    print_theme = current_print_theme_snapshot()
    style = built_in_style_projection("bowhead-editorial")
    snapshot = style["resolvedPresetSnapshot"]
    return build_chart_style_export_theme_v2(
        {
            "schemaVersion": 2,
            "registryVersion": 0,
            "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
            "style": style,
            "selection": {"source": "product"},
            "accessibility": {
                "contrast": "standard",
                "colorVision": "standard",
                "source": {
                    "contrast": "product",
                    "colorVision": "product",
                },
            },
            "modeColors": {
                "light": actual_surface_projection(
                    snapshot,
                    mode="light",
                    surface=PRINT_PAPER,
                    color_scheme_checksum=print_theme["checksum"],
                ),
                "dark": actual_surface_projection(
                    snapshot,
                    mode="dark",
                    surface=DARK_STANDARD_SURFACE,
                    color_scheme_checksum=print_theme["checksum"],
                ),
            },
            "teamPolicy": {"etag": chart_style_policy_etag_v2(None)},
        },
        print_theme=print_theme,
    )


def _block_v2() -> ExportIRBlock:
    base = _block(rows=[{"region": "APAC", "revenue": 120}])
    spec = base.content["payload"]["spec"]
    frozen = freeze_chart_presentation_v2(
        spec,
        theme=_theme_v2(),
        locale="zh-CN",
    )
    return replace(
        base,
        content={
            **base.content,
            "frozen_chart_presentation": frozen,
        },
    )


def _renderer(
    tmp_path: Path,
    *,
    theme: dict | None = None,
) -> PlaywrightVisualRenderer:
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    script = frontend_dir / "render.mjs"
    script.write_text("", encoding="utf-8")
    node = tmp_path / "node"
    node.write_text("", encoding="utf-8")
    return PlaywrightVisualRenderer(
        attempt_dir=tmp_path / "attempt",
        frontend_dir=frontend_dir,
        frontend_base_url="http://127.0.0.1:3000",
        locale="zh-CN",
        timezone="Asia/Singapore",
        theme=theme or _theme(),
        node_binary=node,
        script_path=script,
        print_theme=current_print_theme_snapshot(),
    )


async def test_browser_backend_accepts_only_matching_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _renderer(tmp_path)

    async def fake_run_process(
        *,
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        request = json.loads(Path(command[2]).read_text(encoding="utf-8"))
        assert request["payload"]["visualInputSha256"] == f"sha256:{'a' * 64}"
        assert request["payload"]["locale"] == "zh-CN"
        assert request["payload"]["theme"] == _theme()
        output = io.BytesIO()
        Image.new("RGBA", (1600, 900), (10, 20, 30, 255)).save(
            output,
            format="PNG",
        )
        Path(command[3]).write_bytes(output.getvalue())
        receipt = {
            "mime": "image/png",
            "rendererVersion": EXPORT_VISUAL_RENDERER_VERSION,
            "visualInputSha256": f"sha256:{'a' * 64}",
            "width": 1600,
            "height": 900,
            "warnings": [],
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(receipt).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(
        "bowhead_core.export_browser_visual._run_browser_process",
        fake_run_process,
    )

    result = await renderer.render(
        _block(rows=[{"region": "APAC", "revenue": 120}]),
        asset_paths={"art_rows": tmp_path / "pinned.csv"},
    )

    assert result.mime == "image/png"
    assert result.visual_input_sha256 == f"sha256:{'a' * 64}"
    assert not (tmp_path / "attempt/browser/sec_chart.json").exists()


async def test_browser_backend_requires_v2_surface_and_presentation_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theme = _theme_v2()
    renderer = _renderer(tmp_path, theme=theme)
    block = _block_v2()
    presentation = block.content["frozen_chart_presentation"]

    async def fake_run_process(
        *,
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        request = json.loads(Path(command[2]).read_text(encoding="utf-8"))
        payload = request["payload"]
        assert payload["theme"] == theme
        assert payload["frozenPresentation"] == presentation
        assert payload["exportSurfaceContractVersion"] == CHART_EXPORT_SURFACE_CONTRACT_VERSION
        output = io.BytesIO()
        Image.new("RGBA", (1600, 900), (10, 20, 30, 255)).save(
            output,
            format="PNG",
        )
        Path(command[3]).write_bytes(output.getvalue())
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "mime": "image/png",
                    "rendererVersion": EXPORT_VISUAL_RENDERER_VERSION,
                    "visualInputSha256": block.visual_input_sha256,
                    "exportSurfaceContractVersion": (CHART_EXPORT_SURFACE_CONTRACT_VERSION),
                    "presentationChecksum": presentation["presentationChecksum"],
                    "width": 1600,
                    "height": 900,
                    "warnings": [],
                }
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(
        "bowhead_core.export_browser_visual._run_browser_process",
        fake_run_process,
    )

    result = await renderer.render(block, asset_paths={})

    assert result.visual_input_sha256 == block.visual_input_sha256


async def test_browser_backend_rejects_incomplete_v2_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _renderer(tmp_path, theme=_theme_v2())
    block = _block_v2()

    async def fake_run_process(
        *,
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        output = io.BytesIO()
        Image.new("RGBA", (1600, 900), (10, 20, 30, 255)).save(
            output,
            format="PNG",
        )
        Path(command[3]).write_bytes(output.getvalue())
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "mime": "image/png",
                    "rendererVersion": EXPORT_VISUAL_RENDERER_VERSION,
                    "visualInputSha256": block.visual_input_sha256,
                    "width": 1600,
                    "height": 900,
                    "warnings": [],
                }
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(
        "bowhead_core.export_browser_visual._run_browser_process",
        fake_run_process,
    )

    with pytest.raises(
        BrowserVisualBackendError,
        match="visual_browser_receipt_mismatch",
    ):
        await renderer.render(block, asset_paths={})


async def test_browser_backend_never_fetches_live_artifact_fallback(
    tmp_path: Path,
) -> None:
    renderer = _renderer(tmp_path)

    with pytest.raises(
        BrowserVisualBackendError,
        match="visual_pinned_data_resolver_required",
    ):
        await renderer.render(
            _block(rows=None),
            asset_paths={"art_rows": tmp_path / "pinned.csv"},
        )


def test_browser_backend_rejects_remote_or_credentialed_origin(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        BrowserVisualBackendError,
        match="visual_browser_config_invalid",
    ):
        PlaywrightVisualRenderer(
            attempt_dir=tmp_path / "attempt",
            frontend_dir=tmp_path,
            frontend_base_url="https://user:secret@example.com?leak=1",
            locale="zh-CN",
            timezone="Asia/Singapore",
            theme=_theme(),
            node_binary=tmp_path / "missing-node",
            script_path=tmp_path / "missing-script",
            print_theme=current_print_theme_snapshot(),
        )


@pytest.mark.skipif(
    os.name != "posix",
    reason="process-group ownership is a POSIX deployment contract",
)
async def test_browser_timeout_kills_descendant_process_group(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time,pathlib;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid));"
        "time.sleep(60)"
    )

    with pytest.raises(TimeoutError):
        await browser_visual_module._run_browser_process(  # noqa: SLF001
            command=(
                sys.executable,
                "-c",
                code,
                str(child_pid_path),
            ),
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            # Timeout must follow grandchild startup and PID persistence: under full parallel load, 0.2s often expires before
            # the interpreter starts, killing the parent before the PID file exists (run1: FileNotFoundError). This checks
            # cleanup of the entire process group after timeout, not the timeout duration itself.
            timeout_seconds=3,
        )

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    async with asyncio.timeout(2):
        while _process_exists(child_pid):
            await asyncio.sleep(0.02)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("locale", ["de-DE", "fr-FR"])
def test_chart_export_context_accepts_german_and_french(locale: str) -> None:
    """导出视觉渲染支持四种界面语言（德语此前在渲染脚本里被拒成 visual_request_invalid）。"""
    from bowhead_core.chart_style_export import ChartStyleExportError, export_chart_render_context_v2

    assert export_chart_render_context_v2(locale=locale)["locale"] == locale
    with pytest.raises(ChartStyleExportError):
        export_chart_render_context_v2(locale="ja-JP")

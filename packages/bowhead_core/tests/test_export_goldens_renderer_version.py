"""R7：golden 基线不许比 renderer version 旧。

`deploy/canvas-export-visual-smoke.sh` 的政策是「`EXPORT_VISUAL_RENDERER_VERSION`
bump 或有意视觉改版 ⇒ `--regenerate` 重生成基线并同 commit」。这条政策此前**纯靠
人记**，实测从 2026-08-01 起被破了三次（v2→v3 由 `c80341f3d`、v3→v4 由
`0ec948c82`、v4→v5 由 `c03b48f72`）都没人发现——直到 build 期的像素闸挂掉才暴露，
那时已经积了三代的差，还顺带盖住了一个真缺陷（标签底片全黑，见
`lib/chart/color.ts`）。

这道闸把「基线过期」从**镜像内像素比对**提前到**本机 pytest**：改了版本号不重生成，
这里立刻红，不用等构建。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bowhead_core.export_visual_identity import EXPORT_VISUAL_RENDERER_VERSION

_GOLDEN_DIR = Path(__file__).resolve().parents[3] / "deploy" / "export-goldens"
_MANIFEST = _GOLDEN_DIR / "RENDERER_VERSION"


@pytest.mark.unit
def test_goldens_carry_a_renderer_version() -> None:
    assert _MANIFEST.is_file(), f"{_MANIFEST} 缺失——基线目录必须记下它是哪一代 renderer 生成的，否则「基线过期」只能等镜像内的像素闸才发现"


@pytest.mark.unit
def test_goldens_match_current_renderer_version() -> None:
    recorded = _MANIFEST.read_text(encoding="utf-8").strip()
    assert recorded == EXPORT_VISUAL_RENDERER_VERSION, (
        f"golden 基线是 renderer v{recorded} 生成的，而当前是 "
        f"v{EXPORT_VISUAL_RENDERER_VERSION}。bump 版本号就必须在**同一个提交**里跑一次\n"
        "  docker run --rm <app 镜像> /usr/local/bin/canvas-export-visual-smoke --regenerate\n"
        "并把 deploy/export-goldens/ 下重生成的 PNG 一起提交（禁手工截图）。\n"
        "★ 别为了让这条变绿就盲目重生成：那等于把当前状态（含尚未发现的视觉回退）"
        "钉成新基线。先把新旧图摆一起看过，确认差异只有你有意做的那些。"
    )


@pytest.mark.unit
def test_every_fixture_has_a_baseline_png() -> None:
    fixtures = sorted(p.stem for p in _GOLDEN_DIR.glob("spec_*.json"))
    pngs = sorted(p.stem for p in _GOLDEN_DIR.glob("spec_*.png"))
    assert fixtures, "golden fixture 一个都没有——这道闸等于没上"
    assert fixtures == pngs, f"fixture 与基线 PNG 不成对：只有 json 的 {set(fixtures) - set(pngs)}"

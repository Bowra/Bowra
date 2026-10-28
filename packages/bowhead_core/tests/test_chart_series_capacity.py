"""序列上限与前端调色板槽位必须相等（chart-config-ux 批4 ⑨）。

后端用 `_MAX_SERIES` 决定「几条序列还画得出」，而「画得出」的物理约束在前端：
`lib/chart-theme.ts` 的调色板有几个色，就最多有几条**能区分**的序列。
两边各写一个数就会漂移——后端放到 8，前端只有 6 色，多出的两条与前两条同色，
图上看着是四条线其实是六条。

所以这条闸**跨边界读那份 TS 源**逐字对拍。它不测渲染，只测两个数字没分家。

⚠ 这条闸盯的是 `_MAX_SERIES`（**异族**上限）。同族另有 `_MAX_SERIES_SAME_FAMILY`，
**故意不等于色板槽位**——它取两个色板轮次（原色 6 + `seriesColor()` 淡化的 6）。
两个数各有各的依据，不要合并：
  · 异族 = N 个不同指标并排，每条要一个基础色，所以钉在色板槽位上；
  · 同族 = 一个指标切成 N 份，颜色由 `seriesColor()` 延展供给，
    上限来自「第几轮之后色相开始糊」，与基础槽位数无关。

⚠ 1.2.28 dogfood 的教训：这道闸的 `_infer_reason` 原来写「超过调色板 N 槽」，
而 `seriesColor()` 上线后那已不是约束；**模型读了这句话并照做**——把 10 线趋势
交给 matplotlib 出 PNG，还在正文里向用户转述了这个理由。
reason 是给模型看的，措辞就是接口。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bowhead_core.chart.infer import _MAX_SERIES
from bowhead_core.chart_style import PRODUCT_DEFAULT_PRESET_ID
from bowhead_core.chart_style_registry_generated import CHART_STYLE_PRESETS

# packages/bowhead_core/tests/<this file> → parents[2] = packages/
_THEME = Path(__file__).resolve().parents[2] / "bowhead_frontend" / "lib" / "chart-theme.ts"


def _default_id_in_frontend() -> str:
    src = _THEME.read_text(encoding="utf-8")
    m = re.search(r'export const PRODUCT_DEFAULT_CHART_STYLE_ID = "([a-z-]+)" as const;', src)
    assert m, f"PRODUCT_DEFAULT_CHART_STYLE_ID is no longer declared in {_THEME}; update this gate"
    return m.group(1)


def _palette(name: str) -> list[str]:
    """The frontend fallback palettes are read from the product-default preset in the registry."""

    src = _THEME.read_text(encoding="utf-8")
    mode = {"PALETTE_LIGHT": "light", "PALETTE_DARK": "dark"}[name]
    assert re.search(rf"const {name}: string\[\] = \[\.\.\.PRODUCT_DEFAULT_TOKENS\.{mode}\.palette\];", src), (
        f"{name} must be derived from the product-default preset in {_THEME}"
    )
    return list(CHART_STYLE_PRESETS[_default_id_in_frontend()]["tokens"][mode]["palette"])


@pytest.mark.unit
def test_theme_file_is_where_we_think_it_is() -> None:
    """Positive control: a wrong path must fail instead of silently turning the gates below into no-ops."""
    assert _THEME.is_file(), _THEME


@pytest.mark.unit
def test_frontend_default_matches_backend_product_default() -> None:
    assert _default_id_in_frontend() == PRODUCT_DEFAULT_PRESET_ID


@pytest.mark.unit
@pytest.mark.parametrize("name", ["PALETTE_LIGHT", "PALETTE_DARK"])
def test_series_cap_equals_palette_slots(name: str) -> None:
    slots = _palette(name)
    assert len(slots) == _MAX_SERIES, f"{name} has {len(slots)} colors but _MAX_SERIES={_MAX_SERIES}"


@pytest.mark.unit
def test_palette_slots_are_distinct() -> None:
    """Matching slot counts mean nothing unless the colors really differ."""
    for name in ("PALETTE_LIGHT", "PALETTE_DARK"):
        slots = _palette(name)
        assert len({s.lower() for s in slots}) == len(slots), f"{name} repeats a color"

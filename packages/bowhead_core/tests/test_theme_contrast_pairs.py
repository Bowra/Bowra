"""CSS 真的配成「背景 + 前景」的 token 对，必须在每套调色板上都读得出来。

起因（116 生产）：`.v2-send-btn:disabled` 是 `background: var(--c-line)` ＋
`color: var(--c-ink-4)`，而生成式主题里这两个 token 被算成了同一个颜色（#8E8574），
对比度 1.00 —— 发送按钮的禁用图标完全不可见。

根因不在色阶索引，在 `theme_validation._repair_line_and_focus`：它把 WCAG 的**非文本
3:1** 强加到每一个 `line` 上，于是浅色分割线被一路压暗到 ink-4 那一带。手写调色板不走
修复（它的 line 对面只有 1.29），两套调色板因此长成了完全不同的东西 —— 那是视觉政策，
不在本测试的射程内。这里只钉住一件事：**CSS 依赖的那几对 token 必须可读**。
"""

from __future__ import annotations

import pytest
from bowhead_core.legacy_theme_registry import legacy_m1_compiled_scheme
from bowhead_core.theme_compiler import _PRESETS, compile_theme_scheme

# The floor comes from the **handwritten palette's own dark disabled state** (1.70), intentionally dimmed but still present;
# no palette may do worse. Disabled states need not meet body text's 4.5:1, but around 1.2 they are absent rather than faint
# (the faulty generated theme: light 1.00, dark 1.24; fixed: 2.87 / 2.23).
_MIN_DISABLED_CONTRAST = 1.6


def _relative_luminance(value: str) -> float:
    raw = value.lstrip("#")
    channels = [int(raw[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(one: str, other: str) -> float:
    first, second = _relative_luminance(one), _relative_luminance(other)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def _schemes() -> list[tuple[str, str, dict[str, str]]]:
    out: list[tuple[str, str, dict[str, str]]] = []
    for preset_id in sorted(_PRESETS):
        compiled = compile_theme_scheme({"version": 2, "configurationType": "preset", "presetId": preset_id})
        legacy = legacy_m1_compiled_scheme(preset_id)
        for mode in ("light", "dark"):
            out.append((f"generated:{preset_id}", mode, compiled[mode]))
            out.append((f"legacy:{preset_id}", mode, legacy[mode]))
    return out


@pytest.mark.parametrize(("palette", "mode", "scheme"), _schemes())
def test_disabled_control_foreground_is_visible(palette: str, mode: str, scheme: dict[str, str]) -> None:
    """禁用态控件（`.v2-send-btn:disabled`）用的 surface-3 ＋ ink-4 必须读得出来。"""

    contrast = _contrast(scheme["surface3"], scheme["ink4"])

    assert contrast >= _MIN_DISABLED_CONTRAST, (
        f"{palette}/{mode}: surface3={scheme['surface3']} 上的 ink4={scheme['ink4']} 对比度只有 {contrast:.2f} —— 禁用态图标会看不见"
    )


def test_contrast_helper_detects_an_invisible_pair() -> None:
    """正对照：判据本身不是死的 —— 同色必须被判成 1.00 且过不了闸。"""

    assert _contrast("#8E8574", "#8E8574") == pytest.approx(1.0)
    assert _contrast("#8E8574", "#8E8574") < _MIN_DISABLED_CONTRAST

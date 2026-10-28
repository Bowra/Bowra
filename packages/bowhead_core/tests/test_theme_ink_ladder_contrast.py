"""ink 阶梯的可读性守卫 —— feedback-triage-2026-08-24 批 5。

起因：客户报「深色模式下看不清」。分诊时我只把被指到的那一个元素（chat 署名行）
改用 ink-3 就收工了，把 token 失衡记成了另一条。这一批回来做 token，才发现问题
比 CSS 深两层：

  ① `app/globals.css` 的深色阶梯只是**基线默认**。任何配了品牌的团队，
     实际生效的是 `/api/v1/theme/effective` 下发、由 `theme_compiler` 编译出来的
     调色板，经 `brand-runtime.ts` 注入成 `<style id="bowhead-team-brand-tokens">`
     覆盖掉 `--c-ink-3` / `--c-ink-4`（以及别名 `--text-muted` / `--text-subtle`）。
     116 现网实测：编译出来的 dark ink4 = #544C3C on #11100F = **2.24:1**。
     ⇒ 只改 CSS 在两个生产环境上都是**不生效**的。
  ② `theme_validation` 里 WCAG 闸和自动修复**都早就有**，只是矩阵写到 ink2 就停了，
     ink3/ink4 从来不在射程内 —— 「有闸」不等于「闸管这件事」。

所以判据打在**三份实现**上，一份都不能漏：
  · 生成式调色板（compile_theme_scheme → repair_and_validate_theme）
  · legacy 手写调色板（legacy_m1_compiled_scheme，**不过 repair**，只能直接写对）
  · the `app/globals.css` base (since 1.5.0 a verbatim copy of the product default scheme)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from bowhead_core.legacy_theme_registry import legacy_m1_compiled_scheme
from bowhead_core.theme_compiler import _PRESETS, compile_theme_scheme, product_default_compiled_scheme
from bowhead_core.theme_validation import contrast_ratio

# Four text levels (ink / ink2 / ink3 / ink4): WCAG-AA body text requires 4.5:1. ink4 formerly used 3.0;
# UI redesign P0-1 requires its actual 10–12px text to be readable too.
_TEXT_MIN: Final = 4.5
# Old threshold, used only by the regression case proving the historical defect failed even this lower bar.
_MUTED_MIN: Final = 3.0

_REPO = Path(__file__).resolve().parents[3]
_GLOBALS_CSS = _REPO / "packages/bowhead_frontend/app/globals.css"


def _schemes() -> list[tuple[str, str, dict[str, str]]]:
    out: list[tuple[str, str, dict[str, str]]] = []
    for preset_id in sorted(_PRESETS):
        compiled = compile_theme_scheme({"version": 2, "configurationType": "preset", "presetId": preset_id})
        legacy = legacy_m1_compiled_scheme(preset_id)
        for mode in ("light", "dark"):
            out.append((f"generated:{preset_id}", mode, compiled[mode]))
            out.append((f"legacy:{preset_id}", mode, legacy[mode]))
    product_default = product_default_compiled_scheme()
    for mode in ("light", "dark"):
        out.append(("product-default", mode, product_default[mode]))
    return out


@pytest.mark.unit
@pytest.mark.parametrize(("palette", "mode", "scheme"), _schemes())
def test_ink_ladder_is_readable(palette: str, mode: str, scheme: dict[str, str]) -> None:
    """每套调色板、每个模式下，四个 ink 档都要读得出来。

    ⚠ 判据取 background 与 surface 两种底的**最差值** —— 同一个 ink 在 background 上够、
    在 surface 上不够是真会发生的（116 实测 ink3 是 4.42 / 4.08）。
    """
    for role, minimum in (("ink", _TEXT_MIN), ("ink2", _TEXT_MIN), ("ink3", _TEXT_MIN), ("ink4", _TEXT_MIN)):
        worst_backdrop = min(
            ("background", contrast_ratio(scheme[role], scheme["background"])),
            ("surface", contrast_ratio(scheme[role], scheme["surface"])),
            key=lambda pair: pair[1],
        )
        assert worst_backdrop[1] >= minimum, (
            f"{palette} {mode}.{role}={scheme[role]} 在 {worst_backdrop[0]} 上只有 {worst_backdrop[1]:.2f}:1（需 ≥ {minimum}）"
        )


@pytest.mark.unit
@pytest.mark.parametrize(("palette", "mode", "scheme"), _schemes())
def test_ink_ladder_stays_monotonic(palette: str, mode: str, scheme: dict[str, str]) -> None:
    """阶梯必须逐档递减 —— 防「为了过闸把几档一起提亮，结果两档看起来一样」。

    自动修复是按**最小调整**搜索的，它不知道阶梯的存在；ink3 被提亮而 ink4 提得更多
    就会撞档。全绿而层级没了，是这条要拦的东西。
    """
    ratios = [contrast_ratio(scheme[role], scheme["background"]) for role in ("ink", "ink2", "ink3", "ink4")]
    for index in range(len(ratios) - 1):
        assert ratios[index] > ratios[index + 1], f"{palette} {mode} 的 ink 阶梯在第 {index + 1} 档撞档：{[round(r, 2) for r in ratios]}"
    # Raising ink4 to body-text contrast (UI redesign P0-1) leaves strict decrease unable to reject visually identical 4.94 vs 4.93;
    # require at least a 1.15× contrast difference between the two muted levels (same criterion as frontend css-tokens-dark-contrast).
    assert ratios[2] / ratios[3] >= 1.15, f"{palette} {mode} 的 ink3/ink4 几乎同色：{ratios[2]:.2f} vs {ratios[3]:.2f}"


def _css_dark_token(name: str) -> str:
    css = _GLOBALS_CSS.read_text(encoding="utf-8")
    block = next(
        (m.group(1) for m in re.finditer(r'\[data-theme="dark"\]\s*\{([\s\S]*?)\n\}', css) if "--c-ink:" in m.group(1)),
        None,
    )
    assert block is not None, "没找到含 --c-ink 的深色块 —— globals.css 结构变了，请更新本测试"
    found = re.search(rf"{name}\s*:\s*(#[0-9a-fA-F]{{6}})\s*;", block)
    assert found is not None, f"globals.css 深色块里没有字面 hex 的 {name}"
    return found.group(1).upper()


@pytest.mark.unit
@pytest.mark.parametrize("role", ["ink", "ink2", "ink3", "ink4"])
def test_product_default_dark_matches_globals_css(role: str) -> None:
    """The globals.css dark base ladder is the product default scheme's ladder, token for token.

    Since 1.5.0 teams without a color choice are served ``product_default_compiled_scheme``; the CSS
    base is its pre-injection copy, so changing one without the other only hides the drift.
    """
    product_dark = product_default_compiled_scheme()["dark"]
    css_name = {"ink": "--c-ink", "ink2": "--c-ink-2", "ink3": "--c-ink-3", "ink4": "--c-ink-4"}[role]
    assert product_dark[role].upper() == _css_dark_token(css_name), (
        f"product default dark.{role}={product_dark[role]} != globals.css {css_name}={_css_dark_token(css_name)}"
    )


@pytest.mark.unit
def test_contrast_gate_would_catch_the_reported_defect() -> None:
    """positive control：把 116 现网出事那组真值喂进判据，必须判红。

    ★ 用的是从 `/api/v1/theme/effective` 逐字抄回来的真值（team org_0cbf33586c48），
    不是我构造的 fixture —— 否则这条只证明「我编的坏值会红」。
    """
    live_before = {"background": "#11100F", "surface": "#1B1917", "ink3": "#827968", "ink4": "#544C3C"}
    assert contrast_ratio(live_before["ink4"], live_before["background"]) < _MUTED_MIN
    assert contrast_ratio(live_before["ink3"], live_before["surface"]) < _TEXT_MIN

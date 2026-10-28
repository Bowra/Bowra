"""Focus token parity and shared filter chip contrast across every backend palette.

``app/globals.css`` is the default palette when no brand scheme is injected. Its base blocks mirror the product
default scheme (``product_default_compiled_scheme``); its per-profile blocks mirror ``legacy_theme_registry``. The CSS
base set used to omit ``--c-focus``, so focus rings written as ``var(--c-focus)`` disappeared under the default
palette while every brand scheme (generated, legacy, personal) carried a focus role.

The shared filter chip (UI refresh) paints selected text with ``--c-accent-text`` (accent mixed toward ink) on
``accentSoft``. Generated presets and legacy palettes must keep that pair at body-text contrast too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from bowhead_core.legacy_theme_registry import LEGACY_M1_PROFILE_IDS, legacy_m1_compiled_scheme
from bowhead_core.theme_compiler import _PRESETS, compile_theme_scheme, product_default_compiled_scheme
from bowhead_core.theme_validation import contrast_ratio, repair_and_validate_theme

_REPO = Path(__file__).resolve().parents[3]
_GLOBALS_CSS = _REPO / "packages/bowhead_frontend/app/globals.css"
_UI_REFRESH_CSS = _REPO / "packages/bowhead_frontend/styles/ui-refresh.css"
_BODY_TEXT_MIN: Final = 4.5


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)


def _globals_focus(profile_id: str, mode: str) -> str:
    """Return the --c-focus value the default stylesheet applies for one profile (or the base) and mode."""

    css = _strip_comments(_GLOBALS_CSS.read_text(encoding="utf-8"))
    if profile_id == "product-default":
        selector = r":root" if mode == "light" else r'\[data-theme="dark"\]'
        pattern = rf"(?:^|\}})\s*{selector}\s*\{{([^{{}}]*--c-accent:[^{{}}]*)\}}"
    else:
        dark = r'\[data-theme="dark"\]' if mode == "dark" else ""
        pattern = rf':root{dark}\[data-color-profile="{profile_id}"\][^{{]*\{{([^{{}}]*)\}}'
    block = re.search(pattern, css, flags=re.MULTILINE)
    assert block, f"globals.css has no {profile_id} {mode} palette block"
    focus = re.search(r"--c-focus:\s*(#[0-9a-fA-F]{6});", block.group(1))
    assert focus, f"globals.css {profile_id} {mode} block does not define --c-focus"
    return focus.group(1).upper()


def _accent_text_weight() -> float:
    css = _UI_REFRESH_CSS.read_text(encoding="utf-8")
    match = re.search(r"--c-accent-text:\s*color-mix\(in srgb, var\(--c-accent\) (\d+)%, var\(--c-ink\)\);", css)
    assert match, "ui-refresh.css no longer defines --c-accent-text as an accent/ink mix"
    return int(match.group(1)) / 100


def _mix(first: str, second: str, weight: float) -> str:
    a = [int(first[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(second[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * weight + y * (1 - weight)):02X}" for x, y in zip(a, b, strict=True))


def _schemes() -> list[tuple[str, str, dict[str, str]]]:
    out: list[tuple[str, str, dict[str, str]]] = []
    for preset_id in sorted(_PRESETS):
        source = {"version": 2, "configurationType": "preset", "presetId": preset_id}
        compiled = repair_and_validate_theme(source, compile_theme_scheme(source))
        for mode in ("light", "dark"):
            out.append((f"generated:{preset_id}", mode, compiled[mode]))
    for profile_id in sorted(LEGACY_M1_PROFILE_IDS):
        legacy = legacy_m1_compiled_scheme(profile_id)
        for mode in ("light", "dark"):
            out.append((f"legacy:{profile_id}", mode, legacy[mode]))
    product_default = product_default_compiled_scheme()
    for mode in ("light", "dark"):
        out.append(("product-default", mode, product_default[mode]))
    return out


# The terracotta "bowhead" legacy profile has no CSS block: explicit choices render through the injected scheme.
_CSS_LEGACY_PROFILES: Final = sorted(LEGACY_M1_PROFILE_IDS - {"bowhead"})


@pytest.mark.unit
@pytest.mark.parametrize("profile_id", _CSS_LEGACY_PROFILES)
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_globals_focus_matches_legacy_registry(profile_id: str, mode: str) -> None:
    """The default stylesheet and the frozen legacy snapshot declare the same focus color."""

    assert _globals_focus(profile_id, mode) == legacy_m1_compiled_scheme(profile_id)[mode]["focus"]


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_globals_base_focus_matches_product_default(mode: str) -> None:
    """The base palette's focus ring is the product default scheme's focus role (= its accent)."""

    scheme = product_default_compiled_scheme()[mode]
    assert _globals_focus("product-default", mode) == scheme["focus"] == scheme["accent"]


@pytest.mark.unit
@pytest.mark.parametrize(("palette", "mode", "scheme"), _schemes())
def test_selected_filter_chip_text_reaches_body_contrast(palette: str, mode: str, scheme: dict[str, str]) -> None:
    """Selected chip text (--c-accent-text) on accentSoft stays at or above 4.5:1."""

    text = _mix(scheme["accent"], scheme["ink"], _accent_text_weight())
    actual = contrast_ratio(text, scheme["accentSoft"])
    assert actual >= _BODY_TEXT_MIN, f"{palette} {mode}: {text} on {scheme['accentSoft']} is {actual:.2f}:1"

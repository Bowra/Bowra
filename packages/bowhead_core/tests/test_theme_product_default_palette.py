"""The product default palette (SaaS Blue since 1.5.0) is one scheme in three places.

Teams and members without an explicit color choice are served ``product_default_compiled_scheme``
by the effective-theme resolver; ``app/globals.css`` paints the same 24 tokens before any brand
scheme is injected (first load, public pages without a bound entry, resolver unavailable); the
``saas-blue`` built-in palette compiles to the same tokens. These checks keep the copies identical
and keep the default readable in both modes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from bowhead_core.theme_compiler import (
    SCHEME_TOKEN_KEYS,
    compile_theme_scheme,
    product_default_compiled_scheme,
)
from bowhead_core.theme_validation import contrast_ratio

_REPO = Path(__file__).resolve().parents[3]
_GLOBALS_CSS = _REPO / "packages/bowhead_frontend/app/globals.css"
_UI_REFRESH_CSS = _REPO / "packages/bowhead_frontend/styles/ui-refresh.css"

_CSS_NAMES: Final = {
    "background": "--c-bg",
    "surface": "--c-surface",
    "surface2": "--c-surface-2",
    "surface3": "--c-surface-3",
    "line": "--c-line",
    "line2": "--c-line-2",
    "ink": "--c-ink",
    "ink2": "--c-ink-2",
    "ink3": "--c-ink-3",
    "ink4": "--c-ink-4",
    "accent": "--c-accent",
    "accentHover": "--c-accent-hover",
    "accentInk": "--c-accent-ink",
    "accentSoft": "--c-accent-soft",
    "accentTint": "--c-accent-tint",
    "focus": "--c-focus",
    "positive": "--c-pos",
    "positiveSoft": "--c-pos-soft",
    "warning": "--c-warn",
    "warningSoft": "--c-warn-soft",
    "error": "--c-err",
    "errorSoft": "--c-err-soft",
    "info": "--c-info",
    "infoSoft": "--c-info-soft",
}
_TEXT_MIN: Final = 4.5
_UI_MIN: Final = 3.0


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)


def _base_block(mode: str) -> str:
    css = _strip_comments(_GLOBALS_CSS.read_text(encoding="utf-8"))
    selector = r":root" if mode == "light" else r'\[data-theme="dark"\]'
    for match in re.finditer(rf"(?:^|\}})\s*{selector}\s*\{{([^{{}}]*)\}}", css, flags=re.MULTILINE):
        if re.search(r"--c-bg:\s*#", match.group(1)):
            return match.group(1)
    raise AssertionError(f"globals.css has no {mode} base palette block")


def _css_token(block: str, name: str) -> str:
    found = re.search(rf"(?:^|[;\s]){re.escape(name)}:\s*(#[0-9a-fA-F]{{6}});", block)
    assert found, f"base block does not declare {name} as a literal hex"
    return found.group(1).upper()


def _accent_text(scheme: dict[str, str]) -> str:
    css = _UI_REFRESH_CSS.read_text(encoding="utf-8")
    match = re.search(r"--c-accent-text:\s*color-mix\(in srgb, var\(--c-accent\) (\d+)%, var\(--c-ink\)\);", css)
    assert match, "ui-refresh.css no longer defines --c-accent-text as an accent/ink mix"
    weight = int(match.group(1)) / 100
    a = [int(scheme["accent"][i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(scheme["ink"][i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * weight + y * (1 - weight)):02X}" for x, y in zip(a, b, strict=True))


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_globals_base_palette_is_the_product_default_scheme(mode: str) -> None:
    assert set(_CSS_NAMES) == set(SCHEME_TOKEN_KEYS)
    block = _base_block(mode)
    scheme = product_default_compiled_scheme()[mode]
    drift = {key: (scheme[key], _css_token(block, name)) for key, name in _CSS_NAMES.items() if scheme[key] != _css_token(block, name)}
    assert drift == {}, f"globals.css {mode} base drifted from the product default scheme: {drift}"


@pytest.mark.unit
def test_product_default_is_saas_blue_not_terracotta() -> None:
    scheme = product_default_compiled_scheme()
    assert scheme["light"]["accent"] == "#1754DB"
    assert scheme["dark"]["accent"] == "#5F94FF"
    canonical = compile_theme_scheme({"version": 2, "configurationType": "bowhead", "presetId": None})
    legacy_preset = compile_theme_scheme({"version": 2, "configurationType": "preset", "presetId": "bowhead"})
    assert canonical["light"] == scheme["light"] and canonical["dark"] == scheme["dark"]
    # The legacy-compatible terracotta preset is still available for explicit choices.
    assert legacy_preset["light"]["accent"] != scheme["light"]["accent"]


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_product_default_contrast(mode: str) -> None:
    scheme = product_default_compiled_scheme()[mode]
    text_pairs = {
        "accentInk on accent": (scheme["accentInk"], scheme["accent"]),
        "accentInk on accentHover": (scheme["accentInk"], scheme["accentHover"]),
        "accent text on background": (scheme["accent"], scheme["background"]),
        "accent text on surface": (scheme["accent"], scheme["surface"]),
        "selected chip text on accentSoft": (_accent_text(scheme), scheme["accentSoft"]),
    }
    ui_pairs = {
        "focus on background": (scheme["focus"], scheme["background"]),
        "focus on surface": (scheme["focus"], scheme["surface"]),
    }
    failures = [
        f"{label} {contrast_ratio(fg, bg):.2f}:1 < {minimum}"
        for pairs, minimum in ((text_pairs, _TEXT_MIN), (ui_pairs, _UI_MIN))
        for label, (fg, bg) in pairs.items()
        if contrast_ratio(fg, bg) < minimum
    ]
    assert failures == [], f"product default {mode}: {failures}"

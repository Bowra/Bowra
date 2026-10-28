"""Native PDF/PPTX export colors: theme tokens in, contrast guaranteed at render time."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bowhead_core.export_native_palette import (
    NON_TEXT_CONTRAST_MIN,
    TEXT_CONTRAST_MIN,
    NativeExportPalette,
    native_export_palette,
    native_export_palette_from_tokens,
    palette_role_names,
)
from bowhead_core.print_theme import (
    PRINT_PAPER,
    PRINT_THEME_TOKEN_FIELDS,
    PrintThemeError,
    current_print_theme_snapshot,
)
from bowhead_core.theme_validation import contrast_ratio

_SRC = Path(__file__).resolve().parents[1] / "src" / "bowhead_core"

# A future team brand is compiled by the same theme compiler, but the native layouts must not
# rely on that: these accents are deliberately outside the compiler's lightness band.
_PALE_ACCENT = {
    "accent": "#A9C8FF",
    "accentHover": "#C4DAFF",
    "accentInk": "#FFFFFF",
    "accentSoft": "#EEF4FF",
    "accentTint": "#F8FBFF",
}
_DARK_ACCENT = {
    "accent": "#0B1A3C",
    "accentHover": "#050C1E",
    "accentInk": "#FFFFFF",
    "accentSoft": "#1B2A4A",
    "accentTint": "#E9EDF5",
}


def _tokens(**overrides: str) -> dict[str, str]:
    return {**current_print_theme_snapshot()["tokens"], **overrides}


def _assert_every_pair_meets_its_floor(palette: NativeExportPalette) -> None:
    failures = []
    for pair in palette.contrast_pairs():
        foreground = getattr(palette, pair.foreground_role)
        background = getattr(palette, pair.background_role)
        ratio = contrast_ratio(foreground, background)
        if ratio < pair.minimum:
            failures.append(f"{pair.foreground_role} {foreground} on {pair.background_role} {background}: {ratio:.2f} < {pair.minimum}")
    assert failures == []


def test_product_default_palette_comes_from_the_print_theme_tokens() -> None:
    tokens = current_print_theme_snapshot()["tokens"]
    palette = native_export_palette(current_print_theme_snapshot())

    assert palette.page == PRINT_PAPER == "#FFFFFF"
    assert palette.slide == tokens["background"]
    assert palette.ink == tokens["ink"] == "#061223"
    assert palette.muted == tokens["ink3"]
    assert palette.rule == tokens["line"]
    assert palette.accent_bar == palette.accent_text == tokens["accent"] == "#1754DB"
    assert palette.table_header_fill == tokens["accent"]
    assert palette.table_header_ink == tokens["accentInk"]
    assert palette.cover_fill == tokens["ink"] == "#061223"
    assert palette.notice_ink == tokens["warning"]
    # The plain accent reaches only 2.97:1 on the near-black ink cover, so the cover eyebrow
    # and bar move to the compiler's soft accent instead.
    assert palette.cover_eyebrow == palette.cover_bar == tokens["accentSoft"]
    assert palette.cover_title == tokens["accentInk"]
    # No warm neutral from the retired forest/cream/terracotta palettes survives.
    retired = {"#13332B", "#1F4B3E", "#F6F1E7", "#FFFCF6", "#192824", "#65726D", "#C9674B", "#D9E6DE", "#173D32", "#252A27"}
    assert not retired & {getattr(palette, role) for role in palette_role_names()}


def test_product_default_meets_every_contrast_floor() -> None:
    _assert_every_pair_meets_its_floor(native_export_palette(current_print_theme_snapshot()))


@pytest.mark.parametrize("accent", [_PALE_ACCENT, _DARK_ACCENT], ids=["pale_accent", "dark_accent"])
def test_team_accents_outside_the_compiler_band_still_meet_every_floor(accent: dict[str, str]) -> None:
    _assert_every_pair_meets_its_floor(native_export_palette_from_tokens(_tokens(**accent)))


def test_pale_accent_falls_back_to_companion_tokens() -> None:
    palette = native_export_palette_from_tokens(_tokens(**_PALE_ACCENT))
    tokens = _tokens(**_PALE_ACCENT)

    # White text on a pale header band fails, so the header ink becomes the compiled ink;
    # the pale accent and its hover step are both too light for a rule on the slide.
    assert palette.table_header_ink == tokens["ink"]
    assert palette.accent_bar == tokens["ink"]
    assert palette.accent_text == tokens["ink"]
    # On the dark ink cover the pale accent itself is readable, so it stays.
    assert palette.cover_eyebrow == palette.cover_bar == tokens["accent"]


def test_dark_accent_keeps_accent_marks_but_lightens_cover_text() -> None:
    palette = native_export_palette_from_tokens(_tokens(**_DARK_ACCENT))
    tokens = _tokens(**_DARK_ACCENT)

    assert palette.accent_bar == tokens["accent"]
    assert palette.table_header_ink == tokens["accentInk"]
    # Accent text on the (even darker) cover fails; a light companion is chosen.
    assert palette.cover_eyebrow != tokens["accent"]
    assert contrast_ratio(palette.cover_eyebrow, palette.cover_fill) >= TEXT_CONTRAST_MIN


def test_extreme_fallback_when_no_token_passes() -> None:
    # A theme whose every neutral sits on mid-gray leaves no compiler token readable; the
    # palette still resolves to black/white rather than shipping unreadable text.
    gray = dict.fromkeys(PRINT_THEME_TOKEN_FIELDS, "#808080")
    gray["surface"] = gray["background"] = gray["surface2"] = "#FFFFFF"
    palette = native_export_palette_from_tokens(gray)

    assert palette.ink == "#000000"
    _assert_every_pair_meets_its_floor(palette)


def test_team_surface_never_reaches_the_paper() -> None:
    palette = native_export_palette_from_tokens(_tokens(surface="#FFF4E0", background="#F7EEDD"))

    assert palette.page == PRINT_PAPER
    assert palette.card == "#FFF4E0"
    assert palette.slide == "#F7EEDD"


def test_unsatisfiable_theme_fails_closed() -> None:
    # Body ink must read on the white paper and on surface2 at once; a dark surface2 makes that
    # impossible for every candidate, black and white included, so the export is refused
    # rather than rendered with low contrast.
    with pytest.raises(PrintThemeError, match="contrast"):
        native_export_palette_from_tokens(_tokens(surface2="#3A3A3A"))


def test_every_role_is_covered_by_a_contrast_pair() -> None:
    palette = native_export_palette(current_print_theme_snapshot())
    covered = {pair.foreground_role for pair in palette.contrast_pairs()} | {pair.background_role for pair in palette.contrast_pairs()}

    assert covered == set(palette_role_names())
    assert {pair.minimum for pair in palette.contrast_pairs()} == {TEXT_CONTRAST_MIN, NON_TEXT_CONTRAST_MIN}


def test_palette_rejects_an_untrusted_snapshot() -> None:
    snapshot = current_print_theme_snapshot()
    snapshot["tokens"]["ink"] = "#FFFFFF"

    with pytest.raises(PrintThemeError):
        native_export_palette(snapshot)


@pytest.mark.parametrize("module", ["export_pdf.py", "export_pptx.py"])
def test_native_renderers_carry_no_color_literals(module: str) -> None:
    # Every color must come from the palette so a theme change needs no renderer change.
    source = (_SRC / module).read_text(encoding="utf-8")

    assert re.findall(r"#[0-9A-Fa-f]{6}\b", source) == []
    assert re.findall(r"RGBColor\(\s*0x", source) == []
    assert re.findall(r"colors\.(?!HexColor)[a-z]+\b", source) == []

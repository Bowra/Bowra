"""Render-time color roles for the native PDF and PPTX export layouts.

The native layouts never carry their own colors. They receive the frozen print-theme
snapshot of the export job and resolve it into the roles below. Every foreground role is
chosen at render time so that it meets its contrast floor against every background it is
drawn on (WCAG 2: 4.5:1 for text, 3:1 for non-text rules and marks):

* each foreground role lists compiler tokens in order of preference (for example
  ``accentInk`` on an accent fill, ``ink`` on a surface, ``accentHover`` when the accent is
  too light for a rule), and the first candidate that passes wins;
* only if no compiler token passes does the role fall back to black or white, whichever
  contrasts more. Against a single background one of them always passes; a role drawn on
  several backgrounds can still be unsatisfiable when those backgrounds disagree (one light,
  one dark).
* after resolution every pair in ``contrast_pairs()`` is checked again, and a palette that
  misses any floor raises ``PrintThemeError`` (fail closed) instead of exporting low contrast.

The PDF paper (``page``) is always white; team colors reach slides, cards, table headers,
callouts and accents, never the paper.

``NativeExportPalette.contrast_pairs()`` lists every foreground/background pair the renderers
draw; tests walk it for the product default and for deliberately pale and dark accents.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, Final, Literal

from bowhead_core.print_theme import (
    PRINT_PAPER,
    PRINT_THEME_TOKEN_FIELDS,
    PrintThemeError,
    parse_print_theme_snapshot,
)
from bowhead_core.theme_validation import contrast_ratio

TEXT_CONTRAST_MIN: Final = 4.5
NON_TEXT_CONTRAST_MIN: Final = 3.0
_BLACK: Final = "#000000"
_WHITE: Final = "#FFFFFF"

ContrastKind = Literal["text", "non_text"]


@dataclass(frozen=True, slots=True)
class ContrastPair:
    """One foreground drawn on one background by a native renderer."""

    foreground_role: str
    background_role: str
    kind: ContrastKind

    @property
    def minimum(self) -> float:
        return TEXT_CONTRAST_MIN if self.kind == "text" else NON_TEXT_CONTRAST_MIN


@dataclass(frozen=True, slots=True)
class NativeExportPalette:
    """Resolved ``#RRGGBB`` colors for every role the native layouts draw."""

    # Backgrounds and fills (copied from tokens; no contrast floor of their own).
    page: str  # PDF paper, always PRINT_PAPER
    slide: str  # PPTX content-slide background
    card: str  # PPTX cards, table rows, KPI cards
    card_alt: str  # alternating rows/cards, PDF KPI strip and code background
    table_header_fill: str  # PPTX table header band
    soft_header_fill: str  # PDF table header band
    callout_fill: str  # callouts and "view online" cards
    notice_fill: str  # PDF "view online" notice
    cover_fill: str  # PPTX cover slide (the compiled ink, a near-black brand-hued neutral)
    # Text roles.
    ink: str
    heading: str
    soft_header_ink: str
    muted: str
    code_ink: str
    accent_text: str
    table_header_ink: str
    callout_ink: str
    notice_ink: str
    cover_title: str
    cover_body: str
    cover_eyebrow: str
    cover_muted: str
    # Non-text rules and marks.
    rule: str
    accent_bar: str
    callout_border: str
    notice_border: str
    cover_bar: str

    def contrast_pairs(self) -> tuple[ContrastPair, ...]:
        return _CONTRAST_PAIRS


_CONTRAST_PAIRS: Final = (
    # PDF page: body, headings, eyebrow/title, footer rule, notice and callout borders.
    ContrastPair("ink", "page", "text"),
    ContrastPair("heading", "page", "text"),
    ContrastPair("muted", "page", "text"),
    ContrastPair("rule", "page", "non_text"),
    ContrastPair("callout_border", "page", "non_text"),
    ContrastPair("notice_border", "page", "non_text"),
    ContrastPair("ink", "card_alt", "text"),
    ContrastPair("code_ink", "card_alt", "text"),
    ContrastPair("soft_header_ink", "soft_header_fill", "text"),
    ContrastPair("callout_ink", "callout_fill", "text"),
    ContrastPair("notice_ink", "notice_fill", "text"),
    # PPTX content slides: header chrome, page numbers, cards, tables.
    ContrastPair("ink", "slide", "text"),
    ContrastPair("muted", "slide", "text"),
    ContrastPair("accent_text", "slide", "text"),
    ContrastPair("accent_bar", "slide", "non_text"),
    ContrastPair("rule", "slide", "non_text"),
    ContrastPair("ink", "card", "text"),
    ContrastPair("rule", "card", "non_text"),
    ContrastPair("rule", "card_alt", "non_text"),
    ContrastPair("table_header_ink", "table_header_fill", "text"),
    ContrastPair("callout_border", "slide", "non_text"),
    # PPTX cover slide.
    ContrastPair("cover_title", "cover_fill", "text"),
    ContrastPair("cover_body", "cover_fill", "text"),
    ContrastPair("cover_eyebrow", "cover_fill", "text"),
    ContrastPair("cover_muted", "cover_fill", "text"),
    ContrastPair("cover_bar", "cover_fill", "non_text"),
)


def native_export_palette(print_theme: Any) -> NativeExportPalette:
    """Validate a frozen print-theme snapshot and resolve the native layout roles."""

    parsed = parse_print_theme_snapshot(print_theme)
    return native_export_palette_from_tokens(parsed["tokens"])


def native_export_palette_from_tokens(tokens: Mapping[str, str]) -> NativeExportPalette:
    """Resolve roles from print-theme tokens, guaranteeing every contrast floor."""

    missing = [field for field in PRINT_THEME_TOKEN_FIELDS if not isinstance(tokens.get(field), str)]
    if missing:
        raise PrintThemeError(f"print theme tokens are missing: {', '.join(missing)}")
    t = {field: tokens[field].upper() for field in PRINT_THEME_TOKEN_FIELDS}

    page = PRINT_PAPER
    slide = t["background"]
    card = t["surface"]
    card_alt = t["surface2"]
    table_header_fill = t["accent"]
    soft_header_fill = t["accentSoft"]
    callout_fill = t["accentTint"]
    notice_fill = t["warningSoft"]
    cover_fill = t["ink"]

    light_backgrounds = (page, slide, card, card_alt)

    def text(candidates: Sequence[str], *against: str) -> str:
        return _pick((*(t[name] for name in candidates),), against=against, minimum=TEXT_CONTRAST_MIN)

    def mark(candidates: Sequence[str], *against: str) -> str:
        return _pick((*(t[name] for name in candidates),), against=against, minimum=NON_TEXT_CONTRAST_MIN)

    palette = NativeExportPalette(
        page=page,
        slide=slide,
        card=card,
        card_alt=card_alt,
        table_header_fill=table_header_fill,
        soft_header_fill=soft_header_fill,
        callout_fill=callout_fill,
        notice_fill=notice_fill,
        cover_fill=cover_fill,
        ink=text(("ink", "ink2"), *light_backgrounds),
        heading=text(("ink", "ink2"), page),
        soft_header_ink=text(("ink", "ink2"), soft_header_fill),
        muted=text(("ink3", "ink2", "ink"), page, slide),
        code_ink=text(("ink2", "ink"), card_alt),
        accent_text=text(("accent", "accentHover", "ink"), slide),
        table_header_ink=text(("accentInk", "surface", "ink"), table_header_fill),
        callout_ink=text(("ink", "ink2"), callout_fill),
        notice_ink=text(("warning", "ink"), notice_fill),
        # The cover is filled with the compiled ink. Light companions are preferred; the dark
        # ones only matter for a theme whose ink is not dark, which the compiler never emits.
        cover_title=text(("accentInk", "surface", "ink"), cover_fill),
        cover_body=text(("surface2", "accentTint", "surface", "ink2", "ink"), cover_fill),
        cover_eyebrow=text(("accent", "accentSoft", "accentTint", "surface", "ink"), cover_fill),
        cover_muted=text(("accentSoft", "surface3", "surface2", "surface", "ink3", "ink"), cover_fill),
        rule=mark(("line", "ink4", "ink3"), *light_backgrounds),
        accent_bar=mark(("accent", "accentHover", "ink"), slide),
        callout_border=mark(("accent", "accentHover", "ink"), page, slide),
        notice_border=mark(("warning", "ink"), page),
        cover_bar=mark(("accent", "accentSoft", "accentInk", "ink"), cover_fill),
    )
    _require_contrast(palette)
    return palette


def _require_contrast(palette: NativeExportPalette) -> None:
    failures = [
        f"{pair.foreground_role} on {pair.background_role}"
        for pair in palette.contrast_pairs()
        if contrast_ratio(getattr(palette, pair.foreground_role), getattr(palette, pair.background_role)) < pair.minimum
    ]
    if failures:
        raise PrintThemeError(f"print theme cannot meet export contrast: {', '.join(failures)}")


def _pick(candidates: Sequence[str], *, against: Sequence[str], minimum: float) -> str:
    for candidate in candidates:
        if all(contrast_ratio(candidate, background) >= minimum for background in against):
            return candidate
    # No compiler token passes: fall back to the extreme with the better worst-case contrast.
    # Against a single background one of black/white always reaches at least 4.58:1; across
    # disagreeing backgrounds it may not, which _require_contrast then rejects.
    return max(
        (_BLACK, _WHITE),
        key=lambda extreme: min(contrast_ratio(extreme, background) for background in against),
    )


def palette_role_names() -> tuple[str, ...]:
    return tuple(field.name for field in fields(NativeExportPalette))


__all__ = [
    "NON_TEXT_CONTRAST_MIN",
    "TEXT_CONTRAST_MIN",
    "ContrastPair",
    "NativeExportPalette",
    "native_export_palette",
    "native_export_palette_from_tokens",
    "palette_role_names",
]

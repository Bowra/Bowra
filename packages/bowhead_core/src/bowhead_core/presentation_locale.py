"""Canonical locale selection for B1a user-facing source-linkage copy."""

from __future__ import annotations

import re
from typing import Literal

SupportedPresentationLocale = Literal["zh-CN", "en-US", "de-DE", "fr-FR"]
DEFAULT_PRESENTATION_LOCALE: SupportedPresentationLocale = "en-US"
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def canonicalize_supported_locale(value: object) -> SupportedPresentationLocale | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().replace("_", "-")
    if not normalized or _LOCALE_RE.fullmatch(normalized) is None:
        return None
    primary = normalized.split("-", 1)[0].lower()
    if primary == "zh":
        return "zh-CN"
    if primary == "en":
        return "en-US"
    if primary == "de":
        return "de-DE"
    if primary == "fr":
        return "fr-FR"
    return None


def resolve_presentation_locale(*candidates: object) -> SupportedPresentationLocale:
    for candidate in candidates:
        locale = canonicalize_supported_locale(candidate)
        if locale is not None:
            return locale
    return DEFAULT_PRESENTATION_LOCALE


__all__ = [
    "DEFAULT_PRESENTATION_LOCALE",
    "SupportedPresentationLocale",
    "canonicalize_supported_locale",
    "resolve_presentation_locale",
]

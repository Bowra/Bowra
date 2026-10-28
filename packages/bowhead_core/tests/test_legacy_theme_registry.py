from __future__ import annotations

import pytest
from bowhead_core.legacy_theme_registry import (
    is_pre_fix_legacy_m1_compiled_scheme,
    legacy_m1_compiled_scheme,
)


@pytest.mark.unit
def test_legacy_dark_scheme_uses_dark_semantic_status_pairs() -> None:
    dark = legacy_m1_compiled_scheme("bowhead")["dark"]

    assert {
        key: dark[key]
        for key in (
            "positive",
            "positiveSoft",
            "warning",
            "warningSoft",
            "error",
            "errorSoft",
            "info",
            "infoSoft",
        )
    } == {
        "positive": "#62C491",
        "positiveSoft": "#10291F",
        "warning": "#E5B65A",
        "warningSoft": "#30240D",
        "error": "#F58B82",
        "errorSoft": "#351714",
        "info": "#8BB7FF",
        "infoSoft": "#14243E",
    }


@pytest.mark.unit
def test_pre_fix_dark_status_signature_is_detected_precisely() -> None:
    current = legacy_m1_compiled_scheme("bowhead")
    pre_fix = legacy_m1_compiled_scheme("bowhead")
    pre_fix["dark"].update(
        {
            "positive": "#3A724A",
            "positiveSoft": "#E7EFE0",
            "warning": "#A98028",
            "warningSoft": "#F6EBCF",
            "error": "#9F2438",
            "errorSoft": "#F5DADF",
            "info": "#3F6F8C",
            "infoSoft": "#DEEAF1",
        }
    )

    assert is_pre_fix_legacy_m1_compiled_scheme(pre_fix) is True
    assert is_pre_fix_legacy_m1_compiled_scheme(current) is False
    pre_fix["generator"]["version"] = "2"
    assert is_pre_fix_legacy_m1_compiled_scheme(pre_fix) is False

"""Preserve font-specific notices when aggregating third-party licenses."""

import runpy
from pathlib import Path

GENERATOR = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/check_third_party_notices.py"))


def test_font_notices_preserve_each_reserved_name_and_attribution() -> None:
    common = "SIL OPEN FONT LICENSE Version 1.1\n" + "License terms.\n" * 350
    first = "Copyright 2026 First Author\nReserved Font Name 'First'.\n" + common
    second = "Second Author\nReserved Font Name 'Second'.\n" + common
    dependency = GENERATOR["Dep"]
    entries = [
        dependency("vendored", "first", "1", "OFL-1.1", "vendored", license_texts=[("OFL.txt", first)]),
        dependency("vendored", "second", "1", "OFL-1.1", "vendored", license_texts=[("OFL.txt", second)]),
    ]
    rendered = GENERATOR["render_notices"](entries, "sha256:fixture")
    assert first.strip() in rendered
    assert second.strip() in rendered


def test_font_embedding_exception_is_not_merged_into_plain_gpl() -> None:
    common = "GNU GENERAL PUBLIC LICENSE\n" + "Standard terms.\n" * 350
    with_exception = common + "As a special exception, unmodified embedded fonts do not change the document license.\n"
    dependency = GENERATOR["Dep"]
    entries = [
        dependency("vendored", "base-program", "1", "GPL-3.0-or-later", "vendored", license_texts=[("LICENSE", common)]),
        dependency(
            "vendored",
            "embedded-font",
            "1",
            "GPL-3.0-or-later WITH Font-exception-2.0",
            "vendored",
            license_texts=[("LICENSE", with_exception)],
        ),
    ]
    rendered = GENERATOR["render_notices"](entries, "sha256:fixture")
    assert with_exception.strip() in rendered

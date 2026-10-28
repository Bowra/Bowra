"""Display identity from the fixed build, independent of persisted identifiers."""

from bowhead_gateway.edition_extensions import read_build_edition


def product_name() -> str:
    return "Bowra" if read_build_edition() == "community" else "Bowhead"


def product_copy(template: str) -> str:
    """Adapt repository-owned display templates before inserting user content.

    Never pass a persisted value, protocol literal, or rendered user text here.
    """
    return template.replace("Bowhead", product_name())

"""Fixed display identity for Core, independent of licensing and stored content."""

from __future__ import annotations

import json
from pathlib import Path

PRODUCT_IDENTITY_PATH = Path(__file__).with_name("product_identity.json")


def product_name() -> str:
    """Read the product name packaged with the source distribution."""
    identity = json.loads(PRODUCT_IDENTITY_PATH.read_text(encoding="utf-8"))
    if (
        not isinstance(identity, dict)
        or set(identity) != {"schema_version", "product_name"}
        or type(identity["schema_version"]) is not int
        or identity["schema_version"] != 1
        or identity["product_name"] not in ("Bowhead", "Bowra")
    ):
        raise ValueError("Unsupported Core product identity")
    return identity["product_name"]

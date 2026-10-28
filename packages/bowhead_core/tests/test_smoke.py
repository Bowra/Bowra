"""Smoke test — 确保 package 能 import。"""

import pytest


@pytest.mark.unit
def test_import() -> None:
    import bowhead_core

    assert bowhead_core.__version__

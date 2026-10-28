from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


async def test_artifact_id_search_is_exact_and_owner_scoped(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(
        db_path=str(tmp_path / "artifacts.db"),
        backend=LocalBackend(root_dir=tmp_path / "files"),
    )
    await store.initialize()
    owned = await store.put_bytes(
        content=b"owned",
        filename="private-quarterly.csv",
        mime="text/csv",
        created_by="owner_a",
    )
    other = await store.put_bytes(
        content=b"other",
        filename="other-quarterly.csv",
        mime="text/csv",
        created_by="owner_b",
    )

    items, _, _ = await store.list_for_user(user_id="owner_a", q=owned.id)
    leaked, _, _ = await store.list_for_user(user_id="owner_a", q=other.id)

    assert [item.id for item in items] == [owned.id]
    assert leaked == []

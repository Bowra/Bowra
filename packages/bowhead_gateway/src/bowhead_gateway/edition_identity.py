"""Shared identity integration and fail-closed external session compatibility."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from bowhead_core.user_store import UserStore
    from fastapi import FastAPI, Request


class IdentityAdapter(Protocol):
    async def initialize(self, app: FastAPI, db_path: Path) -> None: ...

    async def login_available(self, request: Request) -> bool: ...

    async def session_active(self, app: FastAPI, generation: str) -> bool | None: ...

    async def post_login_metadata(self, app: FastAPI, team_id: str, user_id: str) -> dict[str, Any]: ...


def identity_adapter(app: FastAPI) -> IdentityAdapter | None:
    extensions = getattr(app.state, "gateway_extensions", None)
    return getattr(extensions, "identity", None)


async def external_session_allowed(app: FastAPI, store: UserStore, generation: str) -> bool:
    """Accept external sessions only when their installed adapter validates them."""
    adapter = identity_adapter(app)
    if adapter is not None:
        verdict = await adapter.session_active(app, generation)
        if verdict is not None:
            return verdict
    return not await store.has_external_auth_session(generation)


async def initialize_identity_services(app: FastAPI, db_path: Path) -> None:
    adapter = identity_adapter(app)
    if adapter is not None:
        await adapter.initialize(app, db_path)

"""Operator commands for edition admission and first-owner setup proof."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from bowhead_core.installation import (
    InstallationError,
    community_setup_token,
    initialize_installation,
    preflight_installation,
    read_installation,
)

from bowhead_gateway.edition_extensions import read_build_edition
from bowhead_gateway.settings import get_settings

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


def database_path(url: str) -> str:
    if not url.startswith(("sqlite:///", "sqlite+aiosqlite:///")):
        raise InstallationError("Installation admission requires a SQLite database URL.")
    path = url.split("///", 1)[1]
    if Path(path).name != "bowhead.db":
        raise InstallationError("Installation admission requires the migration-managed bowhead.db path.")
    return path


def prepare_installation(*, db_url: str, migration_command: str) -> None:
    """Refuse incompatible data before migrations, then record admitted provenance."""
    edition = read_build_edition()
    path = database_path(db_url)
    preflight_installation(path, edition)
    if edition == "community":
        # Validate the persistent root before any migration or directory creation.
        from bowhead_core.installation import InstallationIdentity

        community_setup_token(InstallationIdentity("key-validation", "community", "community", False))
    lock_path = Path(path).parent / ".installation-prepare.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fresh = preflight_installation(path, edition)
        subprocess.run([migration_command, "migrate", "upgrade", "head"], check=True, env={**os.environ, "BOWHEAD_DB_URL": db_url})

        async def record() -> None:
            await initialize_installation(path, edition, admitted_fresh=fresh)

        anyio.run(record)


class CommunitySetupMiddleware:
    """Keep pending installations from provisioning accounts through another API."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._complete = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        state = scope["app"].state
        path = scope.get("path", "")
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and (path == root_path or path.startswith(root_path + "/")):
            path = path[len(root_path) :] or "/"
        protected = path == "/api" or path.startswith("/api/") or scope.get("method", "GET") not in ("GET", "HEAD", "OPTIONS")
        allowed = (scope.get("method"), path.rstrip("/")) in {
            ("GET", "/api/v1/bootstrap/status"),
            ("POST", "/api/v1/bootstrap/community"),
        }
        if getattr(state, "edition", "business") == "community" and not self._complete and protected and not allowed:
            store = getattr(state, "user_store", None)
            identity = await anyio.to_thread.run_sync(read_installation, store.db_path) if store else None
            self._complete = identity is not None and identity.current_edition == "community" and identity.setup_complete
            if not self._complete:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    from starlette.responses import JSONResponse

                    await JSONResponse({"detail": {"code": "community_setup_required"}}, status_code=503)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "preflight", "setup-token"))
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--migration-command", default="bowhead")
    args = parser.parse_args()
    url = args.db_url or get_settings().db_url
    path = database_path(url)
    try:
        if args.command == "prepare":
            prepare_installation(db_url=url, migration_command=args.migration_command)
        elif args.command == "preflight":
            preflight_installation(path, read_build_edition())
        else:
            if read_build_edition() != "community":
                raise InstallationError("Setup tokens are available only in a Community build.")
            identity = read_installation(path)
            if identity is None:
                raise InstallationError("Run installation preparation before requesting a setup token.")
            sys.stdout.write(community_setup_token(identity) + "\n")
    except InstallationError as exc:
        parser.exit(1, f"Installation refused: {exc}\n")


if __name__ == "__main__":
    main()

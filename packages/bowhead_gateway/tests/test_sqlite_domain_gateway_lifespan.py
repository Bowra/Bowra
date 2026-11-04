"""Start the actual Gateway with isolated migrated storage and no providers."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import anyio
import pytest
from bowhead_gateway.installation import prepare_installation
from cryptography.fernet import Fernet


@pytest.mark.asyncio
async def test_actual_gateway_lifespan_uses_migrated_domains_without_external_services(tmp_path, monkeypatch):
    from bowhead_gateway import main

    for name in tuple(os.environ):
        if name.startswith("BOWHEAD_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "data/sqlite/bowhead.db"
    settings = {
        "BOWHEAD_DB_URL": f"sqlite:///{database}",
        "BOWHEAD_ENV": "dev",
        "BOWHEAD_FERNET_KEY": Fernet.generate_key().decode(),
        "BOWHEAD_JWT_SECRET": "isolated-gateway-lifespan-root",
        "BOWHEAD_PRIVACY_LEDGER_DB": str(tmp_path / "privacy/deletions.db"),
        "BOWHEAD_KB_CHROMADB_DIR": str(tmp_path / "chroma"),
        "BOWHEAD_VECTOR_DB_ENABLED": "0",
        "ANONYMIZED_TELEMETRY": "False",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    await anyio.to_thread.run_sync(
        lambda: prepare_installation(db_url=settings["BOWHEAD_DB_URL"], migration_command=str(Path(sys.executable).with_name("bowhead")))
    )
    original_connect = socket.socket.connect

    def no_external_transport(connection, address):
        if connection.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("The empty Gateway must not contact external services during this smoke test")
        return original_connect(connection, address)

    monkeypatch.setattr(socket.socket, "connect", no_external_transport)
    application = main.create_app()
    with anyio.fail_after(90):
        async with main.lifespan(application):
            assert Path(application.state.session_store.db_path).name == "bowhead_execution.db"
            assert Path(application.state.artifact_store.db_path).name == "bowhead_content.db"
            assert Path(application.state.user_store.db_path).name == "bowhead.db"

"""Release A Subagent deployment admission CLI contracts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from bowhead_cli.main import app
from bowhead_core.subagent_compatibility import (
    TeamSubagentCompatibilityStore,
)
from bowhead_core.team_subagent_store import AGENT_RELEASE_COMPATIBILITY_VERSION, TeamSubagentStore
from bowhead_core.user_store import UserStore
from typer.testing import CliRunner

runner = CliRunner()


def _initialize(db_path) -> None:
    async def go() -> None:
        users = UserStore(db_path)
        await users.initialize()
        await TeamSubagentStore(db_path).initialize()

    asyncio.run(go())


def _dump(db_path) -> str:
    connection = sqlite3.connect(db_path)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


def test_cli_reports_ready_safe_read_only_admission(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _initialize(db_path)

    async def register() -> None:
        await TeamSubagentCompatibilityStore(db_path).record_heartbeat(
            instance_id="gateway:secret-instance-id",
            reader_version="0001",
            writer_version="0001",
        )

    asyncio.run(register())
    before = _dump(db_path)

    result = runner.invoke(
        app,
        [
            "admin",
            "check-subagent-deployment-admission",
            "--db",
            str(db_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ready"] is True
    assert payload["candidate_compatible"] is True
    assert payload["live_instances"] == 1
    assert payload["compatible_live_instances"] == 1
    assert payload["migration_pending"] is False
    assert "secret-instance-id" not in result.output
    assert _dump(db_path) == before


def test_cli_returns_gate_exit_for_candidate_below_floor(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _initialize(db_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            UPDATE team_subagent_schema_meta
            SET minimum_reader_version = '0002',
                minimum_writer_version = '0002'
            WHERE singleton = 1
            """,
        )
        connection.commit()
    finally:
        connection.close()

    # Explicitly supply a candidate version below the floor rather than relying on the build default happening to be lower:
    # that default follows the team_subagent_store constant; this test checks the gate's decision itself.
    result = runner.invoke(
        app,
        [
            "admin",
            "check-subagent-deployment-admission",
            "--db",
            str(db_path),
            "--reader-version",
            "0001",
            "--writer-version",
            "0001",
            "--json",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["candidate_reader_version"] == "0001"
    assert payload["candidate_compatible"] is False
    assert payload["ready"] is False


def test_cli_defers_missing_metadata_to_alembic(tmp_path) -> None:
    missing_db = tmp_path / "missing-meta.db"
    asyncio.run(UserStore(missing_db).initialize())
    before = _dump(missing_db)
    missing = runner.invoke(
        app,
        [
            "admin",
            "check-subagent-deployment-admission",
            "--db",
            str(missing_db),
            "--json",
        ],
    )
    assert missing.exit_code == 0, missing.output
    payload = json.loads(missing.output)
    assert payload["ready"] is True
    assert payload["migration_pending"] is True
    assert payload["schema_version"] is None
    assert payload["candidate_compatible"] is None
    assert _dump(missing_db) == before


def test_cli_fails_closed_for_malformed_metadata(tmp_path) -> None:

    malformed_db = tmp_path / "malformed-meta.db"
    _initialize(malformed_db)
    connection = sqlite3.connect(malformed_db)
    try:
        connection.execute(
            """
            UPDATE team_subagent_schema_meta
            SET minimum_writer_version = 'main'
            WHERE singleton = 1
            """,
        )
        connection.commit()
    finally:
        connection.close()
    malformed = runner.invoke(
        app,
        [
            "admin",
            "check-subagent-deployment-admission",
            "--db",
            str(malformed_db),
            "--json",
        ],
    )
    assert malformed.exit_code == 1
    assert "team_subagent_schema_meta_invalid" in malformed.output


def test_cli_default_candidate_matches_runtime_registration(tmp_path) -> None:
    """CLI 声明的镜像能力必须与运行时注册 live 实例用的是同一个常量。

    这两处是「同一个事实的两处声明」：`bowhead_gateway.main` 启动时用
    AGENT_RELEASE_COMPATIBILITY_VERSION 注册 live 实例，而本 CLI 在部署准入里
    声明候选镜像的能力。此前 CLI 停在 RELEASE_A_COMPATIBILITY_VERSION="0001"
    没跟着升，于是同一个镜像运行时说自己是 0003、准入检查里说自己是 0001。

    后果不是「少个提示」：`ensure-agent-release-baseline` 一旦跑过就把库的
    minimum_reader/writer 棘轮到 0003（SQL 只升不降），此后任何镜像都过不了这道
    fail-closed 闸 —— 116 实测连当时正在跑的那一版自己都被拒，升级和回滚一起堵死。

    断言走**实际调用**而不是源码文本：CLI 的注释里就写着这个常量名，
    文本包含判断会被自己的注释喂饱（本测试第一版正是这么废掉的）。
    """

    db_path = tmp_path / "bowhead.db"
    _initialize(db_path)

    result = runner.invoke(
        app,
        ["admin", "check-subagent-deployment-admission", "--db", str(db_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["candidate_reader_version"] == AGENT_RELEASE_COMPATIBILITY_VERSION
    assert payload["candidate_writer_version"] == AGENT_RELEASE_COMPATIBILITY_VERSION

    gateway_main = (Path(__file__).resolve().parents[2] / "bowhead_gateway" / "src" / "bowhead_gateway" / "main.py").read_text(encoding="utf-8")
    assert "reader_version=AGENT_RELEASE_COMPATIBILITY_VERSION" in gateway_main, "运行时注册处改了常量，准入 CLI 必须同一次跟着改"

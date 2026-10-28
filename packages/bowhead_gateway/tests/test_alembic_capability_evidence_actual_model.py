"""0018：证据/尝试两表补 `actual_model_id`（可空、追加、不回填）。

按 `docs/design/数据库迁移.md` §开发者工作流第 4 条要求的最小升级测试：
当前发布版本 fixture → 新 head，断言 schema、既有数据、读取路径与重复执行幂等。

为什么要这一列：探针本来就拿到了 provider 自报的 model id，比对不上就判
`model_identity_mismatch`，然后把观测值丢了。116 实测三条失败全是 HTTP 200，运维只看到
「身份不匹配」，无从判断是配置写错了还是我们的严格相等判据太死（厂商常返回带版本号的 id）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations import chain_head

from bowhead_core import migrations

# Never hardcode head (see bowhead_core.migrations.chain_head); this file pinned 0020, failing from 0021 onward.
HEAD_REVISION = chain_head()
PREVIOUS_REVISION = "0017_clarification_invalidation_outbox"
TABLES = ("team_llm_capability_evidence", "team_llm_verification_items")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _upgrade_to_previous(db_path: Path) -> None:
    """按真实 revision 链把库带到 0017（本 revision 的真实起点）。

    此前的做法是先升到 head、把账本改写成 0017 再删列——那会让 0019/0020 等后续
    revision 在已升级的库上重跑（0020 找不到被它自己改过的 ``issuer`` 列而失败）。
    """
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, PREVIOUS_REVISION)


def test_actual_model_column_is_added_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to_previous(db_path)

    with sqlite3.connect(db_path) as connection:
        for table in TABLES:
            assert "actual_model_id" not in _columns(connection, table), f"fixture 未回到 0017 形状: {table}"

    db_url = f"sqlite+aiosqlite:///{db_path}"
    assert upgrade(db_url, target="core") == ("core",)
    # Repeated execution must be idempotent (same image may restart repeatedly).
    assert upgrade(db_url, target="core") == ("core",)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        for table in TABLES:
            assert "actual_model_id" in _columns(connection, table)


def test_existing_rows_survive_and_read_as_not_recorded(tmp_path: Path) -> None:
    """升级前写入的证据必须还在，且新列读作 NULL（=「没记录」，不是「一致」）。"""
    db_path = tmp_path / "bowhead.db"
    _upgrade_to_previous(db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO team_llm_capability_evidence(
                id, team_id, provider_id, provider_display_name_snapshot,
                model_id, capability, config_signature, signature_version,
                testability_contract_version, probe_contract_version,
                probe_execution_policy_version, outcome, safe_code,
                safe_http_status, safe_summary_code, vector_dimension,
                batch_id, item_id, initiated_by, observed_at, fresh_until,
                duration_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "lve_pre_0018",
                "org_1",
                "llmp_1",
                "aliyun_qwen",
                "qwen3.6-plus",
                "chat",
                "sig-1",
                "llm-config-signature-v1",
                "openai-chat-testability-v1",
                "chat-v1",
                "probe-policy-v1",
                "verified",
                "ok",
                200,
                None,
                None,
                "lvb_1",
                "lvi_1",
                "usr_1",
                "2026-08-01T00:00:00Z",
                "2026-08-02T00:00:00Z",
                12,
            ),
        )
        connection.commit()

    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT outcome, safe_code, actual_model_id FROM team_llm_capability_evidence WHERE id = ?",
            ("lve_pre_0018",),
        ).fetchone()

    assert row == ("verified", "ok", None)

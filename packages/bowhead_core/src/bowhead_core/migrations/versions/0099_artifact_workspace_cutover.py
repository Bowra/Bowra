"""artifact_workspace_schema_meta —— 切点水位戳的**列**（空间隔离批4 ②）。

批3（`0096`）给 `artifacts` 补了 `workspace_id`，但**没有回填、没有改授权**：存量行留 NULL，
语义是「推不出归属」。批4 要把「推不出」翻成 fail-close，可直接翻会把切点前的历史文件一起
拦掉——而按 2026-09-17 的口径拍板（`artifact-workspace-backfill.md` §7.0）：
**不为历史买单，只保证 going forward**。⇒ 判定按切点分段，于是需要一个「切点」。

★★ **本 revision 只建表、不打戳**（`activated_at` 留 NULL）。

这一条是 2026-09-17 review 纠正过来的，原设计写反了，理由值得留下：

  · 仓里的同族 idiom **恰恰都不在迁移里打戳**：
    `0025` 只**声明** `agent_marketplace_schema_meta.assignment_cutover_complete_at`，
    并在 INSERT 里显式写 **NULL**，真正写它的是 `agent_assignment_store.py`——而且要等
    回填对账成功之后；`0006` 同样只声明三个 `team_subagent_schema_meta.*_semantics_activated_at`，
    从不 INSERT。`0033` 的账本原话：「Alembic 不设置 marker；baseline apply 完成全量
    reconciliation 后才设置 marker」。
  · 在迁移里取 `now()` 还会撞 `数据库迁移.md`「revision 确定且一次执行，**不读取外部服务或时间**」。
    本文件因此**一个时间都不读**：`updated_at` 用常量（同 `0006` 的 `'1970-01-01T00:00:00+00:00'`）。
  · ★ 最实质的一条：`now()` 记的是「**schema 升级的时刻**」，而判据要的是
    「**写入点全部正确接线的时刻**」。这两件事**今天并不重合**——同一次 review 就查出
    `kb.py` 三个 `put_bytes` 写入点当时既不传 `workspace_id` 也不传 `session_id`。
    用升级时刻当水位，会把那些「接线还没修好时创建的产物」误判成「切点后」而 fail-close。

⇒ 谁来打戳：**不是本 revision，也不是批4**。必须由 app 或一条显式 admin 命令，在确认
   C4（切点后新产物的 NULL 率 ≈ 0，见 §7.5）达标后写一次，并用 `COALESCE` 保证写入后不可再动。
   本批**不提供**该 stamper——没有它，读侧读出 NULL ⇒ 没有水位 ⇒ 一切判为「切点前」
   ⇒ **逐字节今天的行为**（fail-open）。这正是安全方向：**闸翻了也不会误伤，直到有人显式打戳**。

⚠ **不进 `ArtifactStore.initialize()` 的 ALTER 循环**（与 `0096` 的双路维护**不同**，是有意的）：
`数据库迁移.md`「不得新增 Store `initialize()` DDL」。`0096` 破例是因为那一列承载**写入**，
store-only 老库缺列会丢归属；本表只被授权判定**读**，缺表 ⇒ 读出 None ⇒ 安全降级。

**本 revision 不回填、不加列、不改任何业务数据、不改任何授权判定。** 其余七个 KB 目标 no-op。

Revision ID: 0099_artifact_workspace_cutover
Revises: 0098_space_notification_links
Create Date: 2026-09-17
"""

from __future__ import annotations

from alembic import context

revision = "0099_artifact_workspace_cutover"
down_revision = "0098_space_notification_links"
branch_labels = None
depends_on = None

TABLE = "artifact_workspace_schema_meta"
# ⚠ 不能拿 revision id 当 savepoint 名：SQLite 标识符不允许以数字开头
# （`SAVEPOINT 0099_...` 直接 "unrecognized token"，`0096` 实测撞到过）。
SAVEPOINT_NAME = "artifact_workspace_cutover"

# ★ `activated_at` **可空**：NULL = 本实例还没打戳 = 还没有切点。
#   写成 NOT NULL 会逼迁移当场编一个时间出来，正是上面否掉的那件事。
_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  singleton    INTEGER PRIMARY KEY CHECK(singleton = 1),
  activated_at TEXT,
  updated_at   TEXT NOT NULL
)
"""

# 单例行先建出来、marker 留 NULL（同 `0025` 对 assignment_cutover_complete_at 的做法）。
# `updated_at` 用**常量**而不是 `now()`：本 revision 不读时间（`数据库迁移.md`）。
_SEED = f"""
INSERT OR IGNORE INTO {TABLE}(singleton, activated_at, updated_at)
VALUES (1, NULL, '1970-01-01T00:00:00+00:00')
"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    # 空 set = 没有 artifacts 表（非 core 目标 / 极早期库）→ no-op。
    # 探测而不是在这里复制一份建表 SQL 当第二真源（同 `0096`）。
    if not {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(artifacts)").fetchall()}:
        return
    connection.exec_driver_sql(f"SAVEPOINT {SAVEPOINT_NAME}")
    try:
        connection.exec_driver_sql(_DDL)
        connection.exec_driver_sql(_SEED)
        connection.exec_driver_sql(f"RELEASE SAVEPOINT {SAVEPOINT_NAME}")
    except BaseException:
        connection.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {SAVEPOINT_NAME}")
        connection.exec_driver_sql(f"RELEASE SAVEPOINT {SAVEPOINT_NAME}")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")

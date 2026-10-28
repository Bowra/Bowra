"""artifacts 行比对助手。

**为什么不放 conftest**：`conftest` 这个模块名在 `bowhead_core/tests` 和
`bowhead_gateway/tests` 下各有一份，`from conftest import ...` 在两个包一起收集时
会解析到另一个，整批 collection error。用唯一名字的兄弟模块（同
`from test_document_admission_migration import _config` 的既有先例）。
"""

from __future__ import annotations


def artifact_columns(db) -> list[str]:
    """当前 artifacts 表的列名。"""
    return [str(row[1]) for row in db.execute("PRAGMA table_info(artifacts)")]


def artifact_rows_by_column(db, columns: list[str]) -> list[tuple]:
    """按**列名**取 artifacts 行，而不是 `SELECT *`。

    `SELECT *` 的元组会随任何一次加列变长，于是「升级没动既有数据」这类断言会被
    **与它无关的**、纯 additive 的迁移误判成红（0051_artifact_row_count 一次撞红三处）。
    传入升级前的列名，就只比对这条断言真正关心的那些列。
    """
    projection = ",".join(f'"{c}"' for c in columns)
    return db.execute(f"SELECT {projection} FROM artifacts ORDER BY id").fetchall()  # noqa: S608 - Column names come from PRAGMA.

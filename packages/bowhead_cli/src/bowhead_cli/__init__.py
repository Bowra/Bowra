"""Bowhead CLI.

命令：
    bowhead db init             初始化 SQLite schema
    bowhead db migrate          应用 pending migrations
    bowhead workspace bootstrap <id>    新 workspace 初始化（MEMORY.md + BOWHEAD.md）
    bowhead trace dump <session_id>     导出 session trace 供 debug
    bowhead eval run            运行评估集（M2）
"""

__version__ = "0.1.0"

"""Bowhead HTTP Gateway（TD §2.4 / §2.5）。

入口：``main.app`` — FastAPI application。
"""

import httpx2

# shortcut: MCP and other SDKs still import legacy names; remove after they migrate.
httpx2.alias_httpx()


__version__ = "0.1.0"

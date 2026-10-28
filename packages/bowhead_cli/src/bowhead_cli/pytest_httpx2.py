"""Load before pytest's third-party plugins so RESPX shares HTTPX2 types."""

import httpx2

# shortcut: RESPX still imports legacy names; remove when it supports HTTPX2.
httpx2.alias_httpx()

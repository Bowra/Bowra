"""Keep same-user processes out of the gateway's environment and memory.

The application container runs every process as one user: the gateway, and the processes
it starts for untrusted content, such as the export Chromium that renders model-written
HTML for the visual self-check (docs/sprints/visual-self-check-2026-11-16/tech-design.md
§6, decision D5; Chromium has no OS sandbox there). Marking the gateway non-dumpable makes
its ``/proc/<pid>/environ``, ``mem`` and similar files readable only with CAP_SYS_PTRACE,
so a process that escapes a renderer cannot read ``BOWHEAD_FERNET_KEY``, the JWT secret or
other secrets from the gateway. It also disables core dumps of the gateway, which would
contain the same secrets.

Children reset the flag when they execute a new program, so Chromium, LibreOffice and
workers are unaffected. The gateway still reads its own ``/proc/self``; host root
(perf, bpftrace) and new ``docker compose exec`` processes are unaffected.
"""

from __future__ import annotations

import sys
from typing import Any

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


def deny_same_user_inspection(log: Any) -> bool:
    """Mark this process non-dumpable on Linux; True when the flag is set. Never raises."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            log.warning("gateway.process_hardening_failed", errno=ctypes.get_errno())
            return False
        dumpable = libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0)
    except Exception as exc:  # noqa: BLE001 - hardening must never stop the gateway
        log.warning("gateway.process_hardening_failed", error_type=type(exc).__name__)
        return False
    log.info("gateway.process_hardening", dumpable=dumpable)
    return dumpable == 0

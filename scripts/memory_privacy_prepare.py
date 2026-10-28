#!/usr/bin/env python3
"""Prepare Memory permanent deletion after stopping all app writers.

Replays durable deletion receipts first, then rewrites historical SQLite free
pages with secure_delete + VACUUM and records the enablement baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from bowhead_core.memory_privacy import (
    MemoryDestructiveOperationStore,
    PrivacyDeletionLedger,
)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    fernet_key = os.environ.get(args.fernet_key_env, "")
    if not fernet_key:
        raise RuntimeError(
            f"{args.fernet_key_env} must contain the Fernet root key",
        )
    db_path = Path(args.db).expanduser().resolve()
    ledger_path = Path(args.ledger).expanduser().resolve()
    if db_path == ledger_path:
        raise RuntimeError(
            "privacy ledger must be a separate SQLite file",
        )
    ledger = PrivacyDeletionLedger(
        ledger_path,
        hmac_key=fernet_key,
        key_version=args.key_version,
    )
    await ledger.initialize()
    store = MemoryDestructiveOperationStore(
        db_path,
        ledger=ledger,
        fingerprint_key=fernet_key,
    )
    await store.initialize()
    replay = await store.replay_committed_deletions()
    await store.run_secure_delete_baseline()
    recovery = await store.resume_incomplete_operations()
    ready, reason = await store.readiness()
    return {
        **replay,
        **recovery,
        "secure_delete_baseline_complete": True,
        "ready": ready,
        "not_ready_reason": reason or "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        required=True,
        help="Shared Bowhead SQLite path; app writers must be stopped",
    )
    parser.add_argument(
        "--ledger",
        required=True,
        help="External privacy-ledger SQLite path on its independent volume",
    )
    parser.add_argument(
        "--fernet-key-env",
        default="BOWHEAD_FERNET_KEY",
        help="Environment variable containing the Fernet root key",
    )
    parser.add_argument(
        "--key-version",
        default=os.environ.get(
            "BOWHEAD_PRIVACY_LEDGER_KEY_VERSION",
            "v1",
        ),
    )
    result = asyncio.run(_run(parser.parse_args()))
    print(json.dumps(result, sort_keys=True))  # noqa: T201
    if not result["ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

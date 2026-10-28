#!/bin/sh
# Candidate-owned, fail-closed startup. Compose stops the previous app writer
# before this process starts; uvicorn is exec'd only after migration, dry-run,
# idempotent cutover, and a final marker/schema/compatibility verification.
set -eu

BOWHEAD_BIN="${BOWHEAD_STARTUP_BOWHEAD_BIN:-/app/.venv/bin/bowhead}"
PYTHON_BIN="${BOWHEAD_STARTUP_PYTHON_BIN:-/app/.venv/bin/python}"
UVICORN_BIN="${BOWHEAD_STARTUP_UVICORN_BIN:-/app/.venv/bin/uvicorn}"
DATABASE_URL="${BOWHEAD_DB_URL:-sqlite+aiosqlite:////app/.data/sqlite/bowhead.db}"
SOURCE_BUILD="${BOWHEAD_VERSION:-unknown}"

"$PYTHON_BIN" -m bowhead_gateway.installation prepare \
  --db-url "$DATABASE_URL" --migration-command "$BOWHEAD_BIN"
"$BOWHEAD_BIN" admin backfill-agent-assignments \
  --db "$DATABASE_URL" --source-build "$SOURCE_BUILD" --json
"$BOWHEAD_BIN" admin ensure-agent-marketplace-cutover \
  --db "$DATABASE_URL" --source-build "$SOURCE_BUILD" --json
"$BOWHEAD_BIN" admin ensure-agent-marketplace-cutover \
  --db "$DATABASE_URL" --verify-only --json
# M2 release baseline: same shape as the M1 cutover above. Boot is the writer-stop
# window compose already gives us. It reports "skipped" (exit 0) when the inventory
# is not safe to migrate unattended. The app starts so the data can be repaired,
# while legacy mutable Agents without a current release remain unavailable to run.
"$BOWHEAD_BIN" admin ensure-agent-release-baseline \
  --db "$DATABASE_URL" --source-build "$SOURCE_BUILD" --json

exec "$UVICORN_BIN" bowhead_gateway.main:app \
  --host 0.0.0.0 --port 8080 --log-level info --timeout-keep-alive 75

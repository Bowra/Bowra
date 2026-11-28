"""Personal remote MCP: owner-only registry and durable call consent.

Remote annotations never grant execution. Until a platform-reviewed policy is
available all tools are unknown and require the owner's consent on every call,
unless the owner has explicitly allowed that exact tool for the active session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import aiosqlite
import anyio
import anyio.to_process
import structlog
from anyio import BrokenWorkerProcess
from anyio.lowlevel import RunVar
from bowhead_router.types import ToolSchema
from cryptography.fernet import Fernet
from jsonschema import Draft202012Validator
from mcp.types import PaginatedRequestParams

from bowhead_core._db import is_authority_busy, open_pooled_read_ctx, open_shared_ctx, repeat_when_authority_busy
from bowhead_core.agents.prompts.peer_block import sanitize_external_tool_description
from bowhead_core.oauth_token_broker import OAuthGrantRejected, OAuthRefreshUnavailable
from bowhead_core.openapi_tool_source import connect_remote_mcp
from bowhead_core.scrub import scrub
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.ssrf_guard import validate_url
from bowhead_core.tool_confirmation_events import announce_confirmation, resolve_confirmation
from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec

CONFIRM_SECONDS = 180  # one waiting time for every confirmation kind
MAX_RESULT_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 128 * 1024
VALIDATION_SECONDS = 3
# Waiting for the per-loop validation slot is bounded so hostile schemas cannot
# build an unbounded queue in front of other users' calls.
VALIDATION_QUEUE_SECONDS = 15
# Covers anyio's own 20-second READY handshake for a new worker plus the init
# round trip and the jsonschema import; it only fails if start-up is truly stuck.
VALIDATION_WORKER_START_SECONDS = 60
# A (connection, version, tool) whose validation timed out is rejected without
# re-running it until the entry expires or the connection version changes.
HUNG_SCHEMA_TTL_SECONDS = 600
HUNG_SCHEMA_MAX_ENTRIES = 256

log = structlog.get_logger(__name__)


# anyio's worker-process pool is per event loop, so the validation slot is too.
_validation_slot: RunVar[anyio.Lock] = RunVar("personal_mcp_validation_slot")
# Per-process; keys never contain arguments.
_hung_schemas: OrderedDict[tuple[str, int, str], float] = OrderedDict()


class ValidationUnavailable(Exception):
    """Validation could not finish in time; the handler reports personal_mcp_call_failed."""


def _slot() -> anyio.Lock:
    try:
        return _validation_slot.get()
    except LookupError:
        slot = anyio.Lock()
        _validation_slot.set(slot)
        return slot


def _schema_known_hung(key: tuple[str, int, str]) -> bool:
    expires = _hung_schemas.get(key)
    if expires is None:
        return False
    if expires <= time.monotonic():
        _hung_schemas.pop(key, None)
        return False
    return True


def _remember_hung_schema(key: tuple[str, int, str]) -> None:
    _hung_schemas.pop(key, None)
    _hung_schemas[key] = time.monotonic() + HUNG_SCHEMA_TTL_SECONDS
    while len(_hung_schemas) > HUNG_SCHEMA_MAX_ENTRIES:
        _hung_schemas.popitem(last=False)


def _log_unavailable(stage: str, started: float, connection_id: str, tool_name: str) -> None:
    log.warning(
        "personal_mcp.validation_unavailable",
        stage=stage,
        duration_ms=int((time.perf_counter() - started) * 1000),
        connection_id=connection_id,
        tool=tool_name,
    )


async def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any], key: tuple[str, int, str]) -> bool:
    """Validate untrusted schema/arguments in a worker process within VALIDATION_SECONDS.

    The remote schema is untrusted (pathological patterns or nesting), so the work runs
    in a cancellable worker process that is killed when the limit passes. The limit
    covers only the validation itself: a trivial call first obtains a started worker
    with the validator imported, so a cold interpreter start on a busy host is never
    charged to the limit. One slot per event loop keeps that warmed worker reserved
    for the bounded call and caps untrusted validation to one CPU per gateway worker;
    waiting for the slot is bounded by VALIDATION_QUEUE_SECONDS.

    The warm pairing relies on personal MCP being the only ``anyio.to_process`` user
    in the gateway: anyio's idle pool is shared and LIFO, so another user could take
    the warmed worker between the two calls. Do not widen the slot to a
    CapacityLimiter for the same reason.

    ``key`` is (connection id, connection version, remote tool name); it is used for
    logs and the hung-schema cache and never includes arguments. Raises
    ValidationUnavailable on a queue, start-up or validation timeout, a broken worker,
    or a schema that recently timed out.
    """
    from bowhead_core.personal_mcp_schema import arguments_valid

    connection_id, _version, tool_name = key
    if _schema_known_hung(key):
        _log_unavailable("known_hung", time.perf_counter(), connection_id, tool_name)
        raise ValidationUnavailable("known_hung")
    slot = _slot()
    started = time.perf_counter()
    try:
        with anyio.fail_after(VALIDATION_QUEUE_SECONDS):
            await slot.acquire()
    except TimeoutError:
        _log_unavailable("queue", started, connection_id, tool_name)
        raise ValidationUnavailable("queue") from None
    try:
        started = time.perf_counter()
        try:
            # Cancelling the caller while anyio waits for a new worker's READY handshake
            # leaves that child process orphaned until the gateway exits (anyio only
            # kills it on non-cancellation failures). The warm limit is above anyio's own
            # 20-second handshake so this stage itself never cancels a handshake.
            with anyio.fail_after(VALIDATION_WORKER_START_SECONDS):
                await anyio.to_process.run_sync(arguments_valid, {"type": "object"}, {}, cancellable=True)
        except (TimeoutError, BrokenWorkerProcess):
            _log_unavailable("warm", started, connection_id, tool_name)
            raise ValidationUnavailable("warm") from None
        started = time.perf_counter()
        try:
            with anyio.fail_after(VALIDATION_SECONDS):
                return await anyio.to_process.run_sync(arguments_valid, schema, arguments, cancellable=True)
        except TimeoutError:
            _remember_hung_schema(key)
            _log_unavailable("validate", started, connection_id, tool_name)
            raise ValidationUnavailable("validate") from None
        except BrokenWorkerProcess:
            _log_unavailable("validate", started, connection_id, tool_name)
            raise ValidationUnavailable("validate") from None
    finally:
        slot.release()


async def personal_mcp_licensed(ctx: ToolExecutionContext) -> bool:
    from bowhead_core.entitlements import deployment_mode_from_env

    if ctx.personal_mcp_license_check is None:
        return deployment_mode_from_env() != "local"
    try:
        return await ctx.personal_mcp_license_check(ctx.team_id or "")
    except Exception:
        return False


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PersonalMcpError(ValueError):
    """Only fixed codes cross an API, event, log or model boundary."""


def _safe(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(sanitize_external_tool_description(value, cap=8000))
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    return value


def normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        raise PersonalMcpError("personal_mcp_invalid_endpoint") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise PersonalMcpError("personal_mcp_invalid_endpoint")
    if any(
        re.fullmatch(r"(?i)(?:api[_-]?key|token|access[_-]?token|secret|password|authorization|signature)", key) for key, _ in parse_qsl(parsed.query)
    ):
        raise PersonalMcpError("personal_mcp_credentials_in_url")
    try:
        validate_url(endpoint)
    except Exception:
        raise PersonalMcpError("personal_mcp_address_blocked") from None
    return endpoint


def auth_headers(auth_type: str, credentials: dict[str, str]) -> dict[str, str]:
    if auth_type == "none":
        return {}
    secret = credentials.get("value", "")
    if not secret or len(secret) > 8192 or "\r" in secret or "\n" in secret:
        raise PersonalMcpError("personal_mcp_invalid_credentials")
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    header = credentials.get("header", "X-API-Key")
    if (
        auth_type != "api_key"
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header)
        or header.lower()
        in {
            "host",
            "cookie",
            "content-length",
            "content-type",
            "connection",
            "transfer-encoding",
            "proxy-authorization",
            "mcp-session-id",
            "mcp-protocol-version",
            "accept",
        }
    ):
        raise PersonalMcpError("personal_mcp_invalid_credentials")
    return {header: secret}


async def discover(endpoint: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """Bound all discovery reads; do not expose remote exceptions or annotations."""
    tools: list[dict[str, Any]] = []
    size = 0
    seen: set[str] = set()
    try:
        with anyio.fail_after(20):
            async with connect_remote_mcp(endpoint, headers, response_bytes_limit=MAX_RESULT_BYTES) as session:
                cursor = None
                for _ in range(10):
                    page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
                    for tool in page.tools:
                        if len(tools) >= 100 or tool.name in seen or len(tool.name) > 128:
                            raise PersonalMcpError("personal_mcp_invalid_tool_list")
                        schema = tool.input_schema
                        Draft202012Validator.check_schema(schema)
                        # No remote schema resolution and no server-controlled
                        # schema instructions. Preserve property names and enums.
                        blob = canonical(schema)
                        if re.search(r'"\$(?:ref|dynamicRef)"\s*:\s*"(?!#)', blob):
                            raise PersonalMcpError("personal_mcp_invalid_schema")

                        def clean_schema(node: Any) -> Any:
                            if isinstance(node, dict):
                                return {
                                    key: (_safe(val) if key in {"description", "title", "$comment"} else clean_schema(val))
                                    for key, val in node.items()
                                    if key != "$id"
                                }
                            if isinstance(node, list):
                                return [clean_schema(item) for item in node]
                            return node

                        if any(
                            secret.removeprefix("Bearer ") and secret.removeprefix("Bearer ") in canonical([tool.name, tool.description, schema])
                            for key, secret in headers.items()
                            if key.lower() != "accept"
                        ):
                            raise PersonalMcpError("personal_mcp_unsafe_metadata")
                        clean = clean_schema(schema)
                        size += len(canonical(clean).encode())
                        if size > MAX_SCHEMA_BYTES:
                            raise PersonalMcpError("personal_mcp_schema_limit")
                        seen.add(tool.name)
                        tools.append(
                            {
                                "remote_name": tool.name,
                                "description": _safe(tool.description or ""),
                                "schema_json": canonical(clean),
                                "schema_digest": digest(clean),
                                "risk": "unknown",
                            }
                        )
                    cursor = page.next_cursor
                    if not cursor:
                        break
                else:
                    raise PersonalMcpError("personal_mcp_invalid_tool_list")
    except PersonalMcpError:
        raise
    except Exception:
        raise PersonalMcpError("personal_mcp_discovery_failed") from None
    return tools


class PersonalMcpStore:
    def __init__(self, db_path: str | Path, *, license_check: Callable[[str], Awaitable[bool]] | None = None) -> None:
        self.db_path = Path(domain_database_path(db_path, "core"))
        self.license_check = license_check

    def _key(self, owner: str, identity: str, purpose: str) -> bytes:
        root = os.environ.get("BOWHEAD_FERNET_KEY", "")
        try:
            raw = base64.urlsafe_b64decode(root.encode())
            if len(raw) != 32:
                raise ValueError
        except Exception:
            raise PersonalMcpError("personal_mcp_key_unavailable") from None
        key = hmac.digest(raw, canonical(["personal-mcp.v1", purpose, owner, identity]).encode(), "sha256")
        return key

    def _cipher(self, owner: str, identity: str, purpose: str) -> Fernet:
        return Fernet(base64.urlsafe_b64encode(self._key(owner, identity, purpose)))

    def seal(self, owner: str, identity: str, purpose: str, value: Any) -> str:
        return self._cipher(owner, identity, purpose).encrypt(canonical(value).encode()).decode()

    def unseal(self, owner: str, identity: str, purpose: str, value: str) -> Any:
        try:
            return json.loads(self._cipher(owner, identity, purpose).decrypt(value.encode()))
        except Exception:
            raise PersonalMcpError("personal_mcp_credentials_unavailable") from None

    async def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with open_pooled_read_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def connection(self, owner: str, connection_id: str) -> dict[str, Any]:
        rows = await self.rows("SELECT * FROM personal_mcp_connection WHERE owner_user_id=? AND id=?", (owner, connection_id))
        if not rows:
            raise PersonalMcpError("personal_mcp_not_found")
        return rows[0]

    async def recover(self) -> None:
        async with open_shared_ctx(self.db_path) as db:
            async with db.execute("SELECT 1 FROM sqlite_schema WHERE name='personal_mcp_invocation'") as cursor:
                present = await cursor.fetchone()
            if not present:
                return
            await db.execute("UPDATE personal_mcp_invocation SET status='unknown',args_cipher=NULL WHERE status='dispatching'")
            await db.execute("UPDATE personal_mcp_invocation SET status='expired',args_cipher=NULL WHERE status IN ('pending','approved')")
            await db.commit()

    async def connections(self, owner: str) -> list[dict[str, Any]]:
        rows = await self.rows(
            "SELECT id,name,endpoint,auth_type,auth_cipher,status,version FROM personal_mcp_connection WHERE owner_user_id=? AND status<>'deleted' ORDER BY created_at",
            (owner,),
        )
        for row in rows:
            cipher = row.pop("auth_cipher")
            if row["auth_type"] == "oauth":
                # Only where the member signs in is shown; the grant itself never leaves the store.
                row["provider_host"] = self._oauth_provider_host(owner, row["id"], cipher) or urlsplit(row["endpoint"]).hostname
            row["tools"] = await self.rows(
                "SELECT id,remote_name,description,risk,selected,schema_digest FROM personal_mcp_tool WHERE connection_id=?", (row["id"],)
            )
            for tool in row["tools"]:
                tool["remote_name"] = _safe(tool["remote_name"])
        return rows

    @repeat_when_authority_busy()
    async def save_connection(
        self,
        owner: str,
        name: str,
        endpoint: str,
        auth_type: str,
        credentials: dict[str, str] | None,
        tools: list[dict[str, Any]],
        *,
        connection_id: str | None = None,
        expected_version: int | None = None,
        keep_unchanged: bool = False,
    ) -> str:
        """Store a connection and its discovered tools.

        ``credentials=None`` keeps the stored credential (a signed-in connection's grant). An explicit
        refresh deselects every tool; ``keep_unchanged`` (signing in again, decision Q1) keeps the
        selection and Agent bindings of tools whose schema did not change. Each attempt rechecks the
        version in its own transaction, so a write that lost the core lease repeats safely.
        """
        identity = connection_id or f"pcm_{uuid4().hex[:20]}"
        cipher = None if credentials is None else self.seal(owner, identity, "credentials", credentials)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if connection_id:
                    if cipher is None:
                        updated = await db.execute(
                            "UPDATE personal_mcp_connection SET name=?,endpoint=?,status='ready',version=version+1,updated_at=? WHERE id=? AND owner_user_id=? AND auth_type=? AND version=? AND status<>'deleted'",
                            (name, endpoint, _now(), identity, owner, auth_type, expected_version),
                        )
                    else:
                        updated = await db.execute(
                            "UPDATE personal_mcp_connection SET name=?,endpoint=?,auth_type=?,auth_cipher=?,status='ready',version=version+1,updated_at=? WHERE id=? AND owner_user_id=? AND version=? AND status<>'deleted'",
                            (name, endpoint, auth_type, cipher, _now(), identity, owner, expected_version),
                        )
                    if updated.rowcount != 1:
                        raise PersonalMcpError("personal_mcp_version_conflict")
                    if keep_unchanged:
                        incoming = {tool["remote_name"]: tool["schema_digest"] for tool in tools}
                        async with db.execute(
                            "SELECT id,remote_name,schema_digest FROM personal_mcp_tool WHERE connection_id=? AND selected=1", (identity,)
                        ) as selected_cursor:
                            lost = [row[0] for row in await selected_cursor.fetchall() if incoming.get(row[1]) != row[2]]
                    else:
                        # Explicit refresh revokes all selections, including tools
                        # with an unchanged schema. The user reconfirms the list.
                        async with db.execute("SELECT id FROM personal_mcp_tool WHERE connection_id=?", (identity,)) as all_cursor:
                            lost = [row[0] for row in await all_cursor.fetchall()]
                    if lost:
                        await db.execute(
                            f"UPDATE personal_mcp_tool SET selected=0 WHERE id IN ({','.join('?' * len(lost))})",  # noqa: S608 - placeholders only
                            tuple(lost),
                        )
                        await db.execute(
                            """UPDATE personal_mcp_binding SET
                          tool_ids_json=(SELECT json_group_array(value) FROM json_each(personal_mcp_binding.tool_ids_json)
                            WHERE value NOT IN (SELECT value FROM json_each(?))), version=version+1
                          WHERE EXISTS(SELECT 1 FROM json_each(personal_mcp_binding.tool_ids_json)
                            WHERE value IN (SELECT value FROM json_each(?)))""",
                            (canonical(lost), canonical(lost)),
                        )
                else:
                    if cipher is None:
                        raise PersonalMcpError("personal_mcp_invalid_credentials")
                    await db.execute(
                        "INSERT INTO personal_mcp_connection VALUES (?,?,?,?,?,?,'ready',1,?,?)",
                        (identity, owner, name, endpoint, auth_type, cipher, _now(), _now()),
                    )
                for tool in tools:
                    # A tool keeps its selection only when it survived above (unchanged schema, Q1).
                    await db.execute(
                        """INSERT INTO personal_mcp_tool VALUES (?,?,?,?,?,?,?,0)
                      ON CONFLICT(connection_id,remote_name) DO UPDATE SET description=excluded.description,
                      schema_json=excluded.schema_json,schema_digest=excluded.schema_digest,risk=excluded.risk,
                      selected=CASE WHEN personal_mcp_tool.schema_digest=excluded.schema_digest THEN personal_mcp_tool.selected ELSE 0 END""",
                        (
                            f"pmt_{uuid4().hex[:20]}",
                            identity,
                            tool["remote_name"],
                            tool["description"],
                            tool["schema_json"],
                            tool["schema_digest"],
                            tool["risk"],
                        ),
                    )
                # Removed tools remain as unselected tombstones for audit IDs;
                # the discovery response identifies the current tool set.
                present = {tool["remote_name"] for tool in tools}
                async with db.execute("SELECT id,remote_name FROM personal_mcp_tool WHERE connection_id=?", (identity,)) as cursor:
                    existing = await cursor.fetchall()
                for tool_id, remote_name in existing:
                    if remote_name not in present:
                        await db.execute(
                            "UPDATE personal_mcp_tool SET risk='unknown',description='[unavailable]',schema_json='{}',schema_digest='',selected=0 WHERE id=?",
                            (tool_id,),
                        )
                await db.execute(
                    "UPDATE personal_mcp_invocation SET status='denied',args_cipher=NULL WHERE connection_id=? AND status IN ('pending','approved')",
                    (identity,),
                )
                await db.execute("DELETE FROM personal_mcp_session_tool_allowance WHERE connection_id=?", (identity,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return identity

    # ── Signed-in connections (OAuth). The sealed auth holds the grant; its ciphertext is the version. ──

    def _oauth_provider_host(self, owner: str, identity: str, cipher: str) -> str | None:
        try:
            meta = self.unseal(owner, identity, "credentials", cipher).get("oauth_meta") or {}
        except PersonalMcpError:
            return None
        return urlsplit(str(meta.get("token_endpoint") or "")).hostname

    async def oauth_grant_versioned(self, owner: str, identity: str) -> tuple[str, dict[str, Any]] | None:
        rows = await self.rows(
            "SELECT auth_cipher FROM personal_mcp_connection WHERE id=? AND owner_user_id=? AND auth_type='oauth' AND status<>'deleted'",
            (identity, owner),
        )
        if not rows or not rows[0]["auth_cipher"]:
            return None
        cipher = str(rows[0]["auth_cipher"])
        return cipher, self.unseal(owner, identity, "credentials", cipher)

    @repeat_when_authority_busy()
    async def create_oauth_connection(self, owner: str, name: str, endpoint: str) -> str:
        """A connection waiting for its first sign-in (status ``authorizing``, no tools)."""
        identity = f"pcm_{uuid4().hex[:20]}"
        cipher = self.seal(owner, identity, "credentials", {})
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "INSERT INTO personal_mcp_connection VALUES (?,?,?,?,'oauth',?,'authorizing',1,?,?)",
                (identity, owner, name, endpoint, cipher, _now(), _now()),
            )
            await db.commit()
        return identity

    @repeat_when_authority_busy()
    async def store_oauth_auth(self, owner: str, identity: str, auth: dict[str, Any]) -> None:
        """Write the grant during sign-in (client registration, code exchange); no version change."""
        cipher = self.seal(owner, identity, "credentials", auth)
        async with open_shared_ctx(self.db_path) as db:
            async with db.execute(
                "UPDATE personal_mcp_connection SET auth_cipher=? WHERE id=? AND owner_user_id=? AND auth_type='oauth' AND status<>'deleted'",
                (cipher, identity, owner),
            ) as cursor:
                changed = cursor.rowcount == 1
            await db.commit()
        if not changed:
            raise PersonalMcpError("personal_mcp_not_found")

    @repeat_when_authority_busy(spent=None, kind="turn")
    async def replace_oauth_grant(self, owner: str, identity: str, auth: dict[str, Any], *, expected_cipher: str) -> bool:
        """Write a renewed grant over ``expected_cipher`` only. A renewal is not a configuration change:
        the version stays, so running Turns keep their manifest."""
        cipher = self.seal(owner, identity, "credentials", auth)
        async with open_shared_ctx(self.db_path) as db:
            async with db.execute(
                "UPDATE personal_mcp_connection SET auth_cipher=? WHERE id=? AND owner_user_id=? AND auth_type='oauth' AND auth_cipher=? AND status<>'deleted'",
                (cipher, identity, owner, expected_cipher),
            ) as cursor:
                changed = cursor.rowcount == 1
            await db.commit()
        return changed

    @repeat_when_authority_busy(spent=None, kind="turn")
    async def end_oauth_grant(self, owner: str, identity: str, auth: dict[str, Any], *, expected_cipher: str) -> None:
        """The service will not renew the sign-in: keep the ended mark and ask the owner to sign in again.

        Fenced by the grant version, so a renewal that failed with an old grant never ends a sign-in the
        owner just completed. Pending calls on the connection are denied, as after any credential failure."""
        cipher = self.seal(owner, identity, "credentials", auth)
        async with open_shared_ctx(self.db_path) as db:
            async with db.execute(
                """UPDATE personal_mcp_connection SET auth_cipher=?,
                  status=CASE WHEN status IN ('ready','unreachable') THEN 'needs_reauth' ELSE status END,
                  version=version+1,updated_at=?
                  WHERE id=? AND owner_user_id=? AND auth_type='oauth' AND auth_cipher=? AND status<>'deleted'""",
                (cipher, _now(), identity, owner, expected_cipher),
            ) as cursor:
                changed = cursor.rowcount == 1
            if changed:
                await db.execute(
                    "UPDATE personal_mcp_invocation SET status='denied',args_cipher=NULL WHERE connection_id=? AND status IN ('pending','approved')",
                    (identity,),
                )
                await db.execute("DELETE FROM personal_mcp_session_tool_allowance WHERE connection_id=?", (identity,))
            await db.commit()

    async def configure(self, owner: str, identity: str, version: int, status: str, selected: list[str]) -> None:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                updated = await db.execute(
                    "UPDATE personal_mcp_connection SET status=?,version=version+1,updated_at=? WHERE id=? AND owner_user_id=? AND version=? AND status<>'deleted'",
                    (status, _now(), identity, owner, version),
                )
                if updated.rowcount != 1:
                    raise PersonalMcpError("personal_mcp_version_conflict")
                async with db.execute("SELECT id FROM personal_mcp_tool WHERE connection_id=? AND schema_digest<>''", (identity,)) as tool_cursor:
                    valid = {row[0] for row in await tool_cursor.fetchall()}
                if not set(selected) <= valid:
                    raise PersonalMcpError("personal_mcp_invalid_selection")
                await db.execute("UPDATE personal_mcp_tool SET selected=0 WHERE connection_id=?", (identity,))
                if status != "deleted":
                    for tool_id in selected:
                        await db.execute("UPDATE personal_mcp_tool SET selected=1 WHERE id=?", (tool_id,))
                else:
                    await db.execute("UPDATE personal_mcp_connection SET auth_cipher='' WHERE id=?", (identity,))
                await db.execute(
                    "UPDATE personal_mcp_invocation SET status='denied',args_cipher=NULL WHERE connection_id=? AND status IN ('pending','approved')",
                    (identity,),
                )
                await db.execute("DELETE FROM personal_mcp_session_tool_allowance WHERE connection_id=?", (identity,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def agents(self, owner: str, team: str) -> list[dict[str, Any]]:
        return await self.rows(
            """SELECT a.id,a.display_name,a.name AS slug,a.live_revision_id AS revision_id,
          COALESCE(b.tool_ids_json,'[]') AS tool_ids_json,COALESCE(b.version,0) AS binding_version
          FROM team_subagent a LEFT JOIN personal_mcp_binding b
            ON b.agent_id=a.id AND b.revision_id=a.live_revision_id
          WHERE a.team_id=? AND COALESCE(a.owner_user_id,a.created_by)=?
            AND a.status='enabled'
            AND a.live_revision_id IS NOT NULL""",
            (team, owner),
        )

    async def bind(self, owner: str, team: str, agent_id: str, revision: str, tool_ids: list[str], expected_version: int) -> None:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """SELECT 1 FROM team_subagent WHERE id=? AND team_id=?
                  AND COALESCE(owner_user_id,created_by)=?
                  AND status='enabled' AND live_revision_id=?""",
                    (agent_id, team, owner, revision),
                ) as cursor:
                    agent = await cursor.fetchone()
                if not agent:
                    raise PersonalMcpError("personal_mcp_not_found")
                async with db.execute(
                    """SELECT t.id FROM personal_mcp_tool t
                  JOIN personal_mcp_connection c ON c.id=t.connection_id
                  WHERE c.owner_user_id=? AND c.status='ready' AND t.selected=1""",
                    (owner,),
                ) as tool_cursor:
                    valid = {row[0] for row in await tool_cursor.fetchall()}
                if not set(tool_ids) <= valid or len(tool_ids) > 20:
                    raise PersonalMcpError("personal_mcp_invalid_selection")
                async with db.execute(
                    "SELECT version FROM personal_mcp_binding WHERE agent_id=? AND revision_id=?", (agent_id, revision)
                ) as cursor_2:
                    old = await cursor_2.fetchone()
                if (old[0] if old else 0) != expected_version:
                    raise PersonalMcpError("personal_mcp_version_conflict")
                await db.execute("DELETE FROM personal_mcp_binding WHERE agent_id=? AND revision_id<>?", (agent_id, revision))
                await db.execute(
                    "UPDATE personal_mcp_invocation SET status='denied',args_cipher=NULL WHERE status IN ('pending','approved') AND execution_id IN (SELECT execution_id FROM personal_mcp_manifest WHERE agent_id=?)",
                    (agent_id,),
                )
                await db.execute(
                    """INSERT INTO personal_mcp_binding VALUES (?,?,?,?,?)
                  ON CONFLICT(agent_id,revision_id) DO UPDATE SET tool_ids_json=excluded.tool_ids_json,version=excluded.version""",
                    (agent_id, revision, owner, canonical(sorted(set(tool_ids))), expected_version + 1),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @repeat_when_authority_busy(spent=None, kind="turn")
    async def manifest(self, owner: str, binding: Any) -> dict[str, Any] | None:
        """Freeze only cached metadata; no network access in task assembly.

        A helper start writes this manifest, a fenced table, so it repeats when a running Turn holds
        the core lease (census T1). Each attempt rechecks the existing (turn, execution) row first.
        """
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM personal_mcp_manifest WHERE turn_id=? AND execution_id=?", (binding.turn_id, binding.execution_id)
                ) as cursor_2:
                    existing = await cursor_2.fetchone()
                if existing:
                    if existing["owner_user_id"] != owner:
                        raise PersonalMcpError("personal_mcp_scope_denied")
                    await db.commit()
                    return dict(existing)
                async with db.execute(
                    """SELECT 1 FROM team_subagent WHERE id=? AND team_id=?
                  AND COALESCE(owner_user_id,created_by)=? AND status='enabled'""",
                    (binding.agent_id, binding.team_id, owner),
                ) as cursor_3:
                    private = await cursor_3.fetchone()
                if not private:
                    await db.commit()
                    return None
                async with db.execute(
                    """SELECT b.* FROM personal_mcp_binding b JOIN team_subagent a ON a.id=b.agent_id
                  WHERE b.agent_id=? AND b.revision_id=? AND b.owner_user_id=? AND b.tool_ids_json<>'[]'
                    AND COALESCE(a.owner_user_id,a.created_by)=? AND a.team_id=?
                    AND a.status='enabled'""",
                    (binding.agent_id, binding.revision_ref, owner, owner, binding.team_id),
                ) as cursor_4:
                    row = await cursor_4.fetchone()
                tools = []
                for tool_id in json.loads(row["tool_ids_json"]) if row else []:
                    async with db.execute(
                        """SELECT t.*,c.name AS connection_name,c.version AS connection_version
                      FROM personal_mcp_tool t JOIN personal_mcp_connection c ON c.id=t.connection_id
                      WHERE t.id=? AND t.selected=1 AND c.owner_user_id=? AND c.status='ready'""",
                        (tool_id, owner),
                    ) as cursor:
                        tool = await cursor.fetchone()
                    if tool is None:
                        async with db.execute(
                            """SELECT 1 FROM personal_mcp_tool t JOIN personal_mcp_connection c ON c.id=t.connection_id
                          WHERE t.id=? AND t.selected=1 AND c.owner_user_id=? AND c.status='needs_reauth'""",
                            (tool_id, owner),
                        ) as stale_cursor:
                            needs_sign_in = await stale_cursor.fetchone()
                        if needs_sign_in is None:
                            raise PersonalMcpError("personal_mcp_binding_unavailable")
                        # The owner must sign in again (or renew the credential): the binding is intact, so
                        # the run keeps its other tools; build_personal_specs offers this one as not runnable.
                        continue
                    tools.append(dict(tool))
                manifest = {
                    "turn_id": binding.turn_id,
                    "execution_id": binding.execution_id,
                    "owner_user_id": owner,
                    "agent_id": binding.agent_id,
                    "revision_id": binding.revision_ref,
                    "binding_version": row["version"] if row else 0,
                    "tools_json": canonical(tools),
                }
                await db.execute("INSERT INTO personal_mcp_manifest VALUES (?,?,?,?,?,?,?)", tuple(manifest.values()))
                await db.commit()
                return manifest
            except BaseException:
                await db.rollback()
                raise

    async def tools_needing_sign_in(self, owner: str, binding: Any) -> list[dict[str, Any]]:
        """Bound tools whose connection needs the owner to sign in again or renew its credential."""
        return await self.rows(
            """SELECT t.id,t.remote_name,t.description,t.schema_json,t.connection_id,c.name AS connection_name,c.auth_type
          FROM personal_mcp_binding b JOIN json_each(b.tool_ids_json) j
          JOIN personal_mcp_tool t ON t.id=j.value JOIN personal_mcp_connection c ON c.id=t.connection_id
          WHERE b.agent_id=? AND b.revision_id=? AND b.owner_user_id=? AND c.owner_user_id=?
            AND c.status='needs_reauth' AND t.selected=1 AND t.schema_digest<>''
          ORDER BY t.id""",
            (binding.agent_id, binding.revision_ref, owner, owner),
        )

    async def revalidate(self, owner: str, manifest: dict[str, Any], tool: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        if not await personal_mcp_licensed(ctx):
            raise PersonalMcpError("personal_mcp_unlicensed")
        if ctx.user_id != owner or ctx.turn_id != manifest["turn_id"]:
            raise PersonalMcpError("personal_mcp_scope_denied")
        rows = await self.rows(
            """SELECT c.* FROM personal_mcp_connection c
          JOIN personal_mcp_tool t ON t.connection_id=c.id
          JOIN personal_mcp_binding b ON b.agent_id=? AND b.revision_id=?
          JOIN team_subagent a ON a.id=b.agent_id
          JOIN turns turn ON turn.id=? JOIN sessions s ON s.id=turn.session_id
          JOIN workspaces w ON w.id=s.workspace_id
          JOIN team_members tm ON tm.team_id=w.team_id AND tm.user_id=? AND tm.status='active'
          JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=tm.user_id AND wm.status='active'
          JOIN users u ON u.id=tm.user_id AND u.status='active'
          JOIN teams team ON team.id=w.team_id AND team.status='active'
          JOIN turn_agent_execution e ON e.id=? AND e.turn_id=turn.id AND e.team_id=w.team_id
            AND e.agent_id=a.id AND e.revision_ref=b.revision_id AND e.agent_source_kind='team'
            AND e.status IN ('admitted','running')
          WHERE c.id=? AND c.owner_user_id=? AND c.status='ready' AND c.version=?
            AND t.id=? AND t.selected=1 AND t.schema_digest=? AND t.risk=?
            AND b.owner_user_id=? AND b.version=? AND EXISTS(SELECT 1 FROM json_each(b.tool_ids_json) WHERE value=t.id)
            AND COALESCE(a.owner_user_id,a.created_by)=? AND a.status='enabled' AND a.team_id=w.team_id
            AND s.user_id=? AND s.id=? AND s.workspace_id=? AND w.team_id=?
            AND w.status='active' AND wm.role<>'viewer' AND turn.status='running'""",
            (
                manifest["agent_id"],
                manifest["revision_id"],
                ctx.turn_id,
                owner,
                manifest["execution_id"],
                tool["connection_id"],
                owner,
                tool["connection_version"],
                tool["id"],
                tool["schema_digest"],
                tool["risk"],
                owner,
                manifest["binding_version"],
                owner,
                owner,
                ctx.session_id,
                ctx.workspace_id,
                ctx.team_id,
            ),
        )
        if not rows:
            raise PersonalMcpError("personal_mcp_binding_unavailable")
        # Keep the platform's current Agent run permission decision in addition
        # to the personal owner check (assignment, use grants, explicit deny).
        from bowhead_core.agents.plan_admission import team_agent_runtime_allowed
        from bowhead_core.team_subagent_store import TeamSubagentStore

        if not await team_agent_runtime_allowed(TeamSubagentStore(self.db_path), team_id=ctx.team_id, user_id=owner, agent_id=manifest["agent_id"]):
            raise PersonalMcpError("personal_mcp_scope_denied")
        return rows[0]

    async def enqueue(self, owner: str, manifest: dict[str, Any], tool: dict[str, Any], call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        identity = f"pmi_{uuid4().hex}"
        # Keyed summary avoids a dictionary attack on small or sensitive values.
        cipher = self._cipher(owner, identity, "arguments")
        args_blob = canonical(args)
        if len(args_blob.encode()) > 64 * 1024:
            raise PersonalMcpError("personal_mcp_arguments_limit")
        args_hash = hmac.new(self._key(owner, tool["connection_id"], "argument-summary"), args_blob.encode(), hashlib.sha256).hexdigest()
        expires = (datetime.now(UTC) + timedelta(seconds=CONFIRM_SECONDS)).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT OR IGNORE INTO personal_mcp_invocation VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,NULL)",
                (
                    identity,
                    owner,
                    manifest["turn_id"],
                    manifest["execution_id"],
                    call_id,
                    tool["id"],
                    tool["connection_id"],
                    tool["connection_version"],
                    tool["schema_digest"],
                    args_hash,
                    cipher.encrypt(args_blob.encode()).decode(),
                    _now(),
                    expires,
                ),
            )
            await db.commit()
        row = (
            await self.rows(
                "SELECT * FROM personal_mcp_invocation WHERE owner_user_id=? AND turn_id=? AND execution_id=? AND call_id=?",
                (owner, manifest["turn_id"], manifest["execution_id"], call_id),
            )
        )[0]
        if row["args_digest"] != args_hash or row["tool_id"] != tool["id"]:
            raise PersonalMcpError("personal_mcp_call_changed")
        return row

    async def session_allows(self, owner: str, session_id: str, tool: dict[str, Any]) -> bool:
        rows = await self.rows(
            """SELECT 1 FROM personal_mcp_session_tool_allowance
              WHERE owner_user_id=? AND session_id=? AND connection_id=? AND tool_id=?""",
            (owner, session_id, tool["connection_id"], tool["id"]),
        )
        return bool(rows)

    async def approve_for_session(self, owner: str, identity: str, session_id: str) -> bool:
        """Approve this call and permit the exact tool for the current session."""
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """SELECT i.connection_id,i.tool_id,s.id FROM personal_mcp_invocation i
                          JOIN turns t ON t.id=i.turn_id JOIN sessions s ON s.id=t.session_id
                          WHERE i.id=? AND i.owner_user_id=? AND i.status='pending' AND i.expires_at>?""",
                    (identity, owner, _now()),
                ) as cursor:
                    invocation = await cursor.fetchone()
                if not invocation or invocation[2] != session_id:
                    await db.rollback()
                    return False
                approved = await db.execute(
                    """UPDATE personal_mcp_invocation SET status='approved'
                      WHERE id=? AND owner_user_id=? AND status='pending' AND expires_at>?""",
                    (identity, owner, _now()),
                )
                if approved.rowcount != 1:
                    await db.rollback()
                    return False
                await db.execute(
                    """INSERT OR IGNORE INTO personal_mcp_session_tool_allowance
                      (owner_user_id,session_id,connection_id,tool_id,created_at) VALUES (?,?,?,?,?)""",
                    (owner, session_id, invocation[0], invocation[1], _now()),
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def transition(self, owner: str, identity: str, before: str, after: str, *, duration: int | None = None) -> bool:
        async with open_shared_ctx(self.db_path) as db:
            result = await db.execute(
                """UPDATE personal_mcp_invocation SET status=?,duration_ms=?,
              args_cipher=CASE WHEN ? IN ('pending','approved') THEN args_cipher ELSE NULL END
              WHERE id=? AND owner_user_id=? AND status=? AND (? NOT IN ('approved','dispatching') OR expires_at>?)""",
                (after, duration, after, identity, owner, before, after, _now()),
            )
            await db.commit()
            return result.rowcount == 1

    async def expire(self, owner: str) -> None:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "UPDATE personal_mcp_invocation SET status='expired',args_cipher=NULL WHERE owner_user_id=? AND status IN ('pending','approved') AND expires_at<=?",
                (owner, _now()),
            )
            await db.commit()

    @repeat_when_authority_busy(spent=None, kind="turn")
    async def connection_failure(self, owner: str, identity: str, version: int, status: str) -> None:
        """Mark a connection that failed a dispatched call; the version fence makes a repeat a no-op (census T2)."""
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "UPDATE personal_mcp_connection SET status=?,version=version+1,updated_at=? WHERE id=? AND owner_user_id=? AND version=? AND status='ready'",
                (status, _now(), identity, owner, version),
            )
            await db.execute(
                "UPDATE personal_mcp_invocation SET status='denied',args_cipher=NULL WHERE connection_id=? AND status IN ('pending','approved')",
                (identity,),
            )
            await db.execute("DELETE FROM personal_mcp_session_tool_allowance WHERE connection_id=?", (identity,))
            await db.commit()

    async def pending(self, owner: str) -> list[dict[str, Any]]:
        await self.expire(owner)
        rows = await self.rows(
            """SELECT i.*,c.name AS connection_name,t.remote_name,t.description,
          a.display_name AS agent_name,s.id AS session_id,s.workspace_id,w.team_id,
          m.agent_id,m.revision_id,m.binding_version,m.tools_json FROM personal_mcp_invocation i
          JOIN personal_mcp_connection c ON c.id=i.connection_id JOIN personal_mcp_tool t ON t.id=i.tool_id
          JOIN personal_mcp_manifest m ON m.turn_id=i.turn_id AND m.execution_id=i.execution_id
          JOIN team_subagent a ON a.id=m.agent_id JOIN turns turn ON turn.id=i.turn_id
          JOIN sessions s ON s.id=turn.session_id JOIN workspaces w ON w.id=s.workspace_id
          WHERE i.owner_user_id=? AND i.status='pending' AND turn.status='running'""",
            (owner,),
        )
        result = []
        for row in rows:
            ctx = ToolExecutionContext(
                user_id=owner,
                personal_mcp_license_check=self.license_check,
                turn_id=row["turn_id"],
                workspace_id=row["workspace_id"],
                session_id=row["session_id"],
                team_id=row["team_id"],
                member_uid="",
                datarc=None,
            )
            tool = next(item for item in json.loads(row["tools_json"]) if item["id"] == row["tool_id"])
            try:
                await self.revalidate(owner, row, tool, ctx)
            except PersonalMcpError:
                await self.transition(owner, row["id"], "pending", "denied")
                continue
            result.append(
                {
                    "id": row["id"],
                    "turn_id": row["turn_id"],
                    "session_id": row["session_id"],
                    "connection_name": row["connection_name"],
                    "tool_name": _safe(row["remote_name"]),
                    "description": row["description"],
                    "agent_name": row["agent_name"],
                    "expires_at": row["expires_at"],
                    "arguments": self.unseal(owner, row["id"], "arguments", row["args_cipher"]),
                }
            )
        return result


class _PersonalConnectionGrant:
    """A signed-in personal connection's grant for the shared OAuth broker (``mcp_oauth.OAuthGrantRecord``)."""

    def __init__(self, store: PersonalMcpStore, owner: str, identity: str) -> None:
        self._store = store
        self._owner = owner
        self._identity = identity
        self.cache_scope = f"{store.db_path}:{_grant_key(owner, identity)}"

    async def read_versioned(self) -> tuple[str, dict[str, Any]] | None:
        return await self._store.oauth_grant_versioned(self._owner, self._identity)

    async def replace(self, auth: dict[str, Any], *, expected_version: str) -> bool:
        return await self._store.replace_oauth_grant(self._owner, self._identity, auth, expected_cipher=expected_version)

    async def end(self, auth: dict[str, Any], *, expected_version: str) -> None:
        await self._store.end_oauth_grant(self._owner, self._identity, auth, expected_cipher=expected_version)


class PersonalOAuthAuthStore:
    """``mcp_oauth.FernetTokenStorage``'s store over personal connections: team_id → owner, source_id → connection."""

    def __init__(self, store: PersonalMcpStore) -> None:
        self._store = store
        self.db_path = store.db_path

    async def get_team_tool_auth_decrypted(self, owner: str, identity: str) -> dict[str, Any]:
        row = await self._store.oauth_grant_versioned(owner, identity)
        return row[1] if row else {}

    async def update_team_tool_source_auth(self, owner: str, identity: str, *, auth_type: str, auth: dict[str, Any]) -> None:
        await self._store.store_oauth_auth(owner, identity, auth)


def _grant_key(owner: str, identity: str) -> str:
    return f"personal-mcp:{owner}:{identity}"


async def personal_oauth_headers(
    store: PersonalMcpStore, owner: str, identity: str, endpoint: str, *, rejected_token: str | None = None
) -> dict[str, str]:
    """Bearer header of a signed-in connection from the shared single-flight broker.

    Raises ``OAuthGrantRejected`` when the sign-in is missing or ended (the connection is marked
    ``needs_reauth``) and ``OAuthRefreshUnavailable`` when a renewal could not complete."""
    from bowhead_core.mcp_oauth import oauth_broker, oauth_grant_headers

    return await oauth_grant_headers(
        oauth_broker(store), _grant_key(owner, identity), _PersonalConnectionGrant(store, owner, identity), endpoint, rejected_token=rejected_token
    )


def prime_personal_oauth(store: PersonalMcpStore, owner: str, identity: str, access_token: str, expires_in: int | None) -> None:
    """Use the access token of a connection that was just signed in instead of renewing at once."""
    from bowhead_core.mcp_oauth import oauth_broker

    oauth_broker(store).prime(_grant_key(owner, identity), access_token, expires_in)


def _sign_in_result(connection_name: str, failure: OAuthGrantRejected | OAuthRefreshUnavailable) -> dict[str, Any]:
    """Model-facing result of a call that was not sent because of the connection's sign-in."""
    name = _safe(connection_name)
    if isinstance(failure, OAuthGrantRejected):
        return {
            "ok": False,
            "error_kind": "personal_mcp_sign_in_ended",
            "error": (
                f"Not run: the user's sign-in to {name} has ended; the service no longer accepts it. Tell the user to sign in "
                f"again in My Connections, then retry. Do not retry before that."
            ),
        }
    result: dict[str, Any] = {
        "ok": False,
        "error_kind": "personal_mcp_sign_in_renewal_unavailable",
        "error": (
            f"Not run: renewing the sign-in to {name} did not complete because the service "
            + ("is limiting requests" if failure.reason == "rate_limited" else "did not respond")
            + ". The sign-in itself is still valid. Try again later."
        ),
    }
    if failure.retry_after_seconds is not None:
        result["retry_after_seconds"] = failure.retry_after_seconds
    return result


def _needs_sign_in_result(connection_name: str, auth_type: str) -> dict[str, Any]:
    """Result of a bound tool whose connection needs the owner to act before anything can run."""
    if auth_type == "oauth":
        return _sign_in_result(connection_name, OAuthGrantRejected("needs_reauth"))
    return {
        "ok": False,
        "error_kind": "personal_mcp_credential_rejected",
        "error": (
            f"Not run: {_safe(connection_name)} rejected the stored credential. Tell the user to update the credential "
            f"in My Connections, then retry. Do not retry before that."
        ),
    }


def _needs_sign_in_spec(tool: dict[str, Any]) -> ToolSpec:
    """A bound tool kept visible while its connection needs the owner; calling it sends nothing.

    Without it the Agent would not know the tool exists and could only say the service is unreachable
    (PRD P1, decided 2026-11-25: keep the tool, answer on call)."""
    label = f"{tool['connection_name']} · {_safe(tool['remote_name'])}"
    result = _needs_sign_in_result(tool["connection_name"], tool["auth_type"])

    async def handler(args: dict[str, Any], call_ctx: ToolExecutionContext) -> dict[str, Any]:
        return dict(result)

    return ToolSpec(
        schema=ToolSchema(
            name=f"personalmcp.{tool['connection_id']}.{tool['id']}",
            description=(
                f"{label}: {tool['description']}\nUnavailable in this run: the owner must sign in to {tool['connection_name']} again "
                "(or update its credential) in My Connections. Calling it sends nothing and returns that fact."
            ),
            input_schema=json.loads(tool["schema_json"]),
        ),
        handler=handler,
        source="external",
        personal_mcp_label=label,
        allowed_for_roles=frozenset({"analyst"}),
    )


async def _call_remote(endpoint: str, headers: dict[str, str], remote_name: str, args: dict[str, Any]) -> Any:
    with anyio.fail_after(30):
        async with connect_remote_mcp(endpoint, headers, call_timeout=30, response_bytes_limit=MAX_RESULT_BYTES) as session:
            return await session.call_tool(remote_name, args)


def _sign_in_failure(exc: BaseException) -> OAuthGrantRejected | OAuthRefreshUnavailable | None:
    seen: list[BaseException] = [exc]
    while seen:
        item = seen.pop()
        if isinstance(item, (OAuthGrantRejected, OAuthRefreshUnavailable)):
            return item
        if isinstance(item, BaseExceptionGroup):
            seen.extend(item.exceptions)
        elif item.__cause__ is not None:
            seen.append(item.__cause__)
    return None


async def build_personal_specs(store: PersonalMcpStore, owner: str, binding: Any, ctx: ToolExecutionContext) -> list[ToolSpec]:
    if not await personal_mcp_licensed(ctx) or binding is None or binding.agent_source_kind != "team":
        return []
    manifest = await store.manifest(owner, binding)
    if manifest is None:
        return []
    tools = json.loads(manifest["tools_json"])
    if sum(len(tool["schema_json"].encode()) for tool in tools) > MAX_SCHEMA_BYTES:
        raise PersonalMcpError("personal_mcp_schema_limit")
    specs = []
    for tool in tools:
        await store.revalidate(owner, manifest, tool, ctx)

    def make_handler(tool: dict[str, Any]) -> Any:
        async def handler(args: dict[str, Any], call_ctx: ToolExecutionContext) -> dict[str, Any]:
            identity: str | None = None
            dispatched = False
            # The service answered HTTP 401/403: it refused the request, so nothing ran there.
            refused = False
            auth_type = ""
            oauth_headers: dict[str, str] | None = None
            # A confirmation card announced on the step must end with exactly one outcome.
            card_open = False
            started = time.perf_counter()
            from bowhead_core.team_tool_registry_view import is_upstream_auth_error

            try:
                if getattr(call_ctx, "personal_mcp_execution_id", None) != binding.execution_id:
                    raise PersonalMcpError("personal_mcp_scope_denied")
                await store.revalidate(owner, manifest, tool, call_ctx)
                args_blob = canonical(args)
                if len(args_blob.encode()) > 64 * 1024:
                    raise PersonalMcpError("personal_mcp_arguments_limit")
                # Confirm and dispatch the same snapshot even if the caller
                # mutates its input while the owner is deciding.
                args = json.loads(args_blob)
                if not await validate_arguments(
                    json.loads(tool["schema_json"]), args, (tool["connection_id"], tool["connection_version"], tool["remote_name"])
                ):
                    raise PersonalMcpError("personal_mcp_invalid_arguments")
                call_id = getattr(call_ctx, "personal_mcp_call_id", "")
                if not call_id:
                    raise PersonalMcpError("personal_mcp_scope_denied")
                row = await store.enqueue(owner, manifest, tool, call_id, args)
                identity = row["id"]
                if row["status"] in {"dispatching", "ok", "unknown"}:
                    return {
                        "ok": False,
                        "error_kind": "personal_mcp_outcome_unknown",
                        "error": "This call was already dispatched. Check the external service; never retry automatically.",
                    }
                if row["status"] == "pending" and (tool["risk"] == "read" or await store.session_allows(owner, call_ctx.session_id, tool)):
                    await store.transition(owner, identity, "pending", "approved")
                    row["status"] = "approved"
                if row["status"] == "pending":
                    card_open = True
                    await announce_confirmation(
                        call_ctx,
                        confirmation_id=identity,
                        kind="personal_mcp",
                        requester_user_id=owner,
                        title=_safe(tool["remote_name"]),
                        source_label=tool["connection_name"],
                        choices=["reject", "approve", "approve_session"],
                        expires_at=str(row["expires_at"]),
                        category="unknown" if tool["risk"] not in {"external_write", "destructive"} else tool["risk"],
                    )
                with anyio.move_on_after(CONFIRM_SECONDS):
                    while row["status"] == "pending":
                        await anyio.sleep(0.5)
                        if not await personal_mcp_licensed(call_ctx):
                            raise PersonalMcpError("personal_mcp_unlicensed")
                        await store.expire(owner)
                        row = (await store.rows("SELECT * FROM personal_mcp_invocation WHERE id=? AND owner_user_id=?", (identity, owner)))[0]
                if row["status"] != "approved":
                    await store.transition(owner, identity, "pending", "expired")
                    if card_open:
                        card_open = False
                        outcome = (
                            row["status"] if row["status"] in {"rejected", "expired"} else "expired" if row["status"] == "pending" else "unavailable"
                        )
                        await resolve_confirmation(call_ctx, confirmation_id=identity, outcome=outcome)
                    raise PersonalMcpError(
                        "personal_mcp_confirmation_" + (row["status"] if row["status"] in {"rejected", "expired"} else "unavailable")
                    )
                if card_open:
                    card_open = False
                    # The allowance did not exist when this call was queued (it would have been approved
                    # without asking), so finding it now means this decision allowed the session.
                    for_session = await store.session_allows(owner, call_ctx.session_id, tool)
                    await resolve_confirmation(call_ctx, confirmation_id=identity, outcome="approved_session" if for_session else "approved")
                connection = await store.revalidate(owner, manifest, tool, call_ctx)
                auth_type = str(connection["auth_type"])
                await store.expire(owner)
                if connection["auth_type"] == "oauth":
                    # Renew before the call is marked dispatched: a sign-in failure means nothing was sent.
                    oauth_headers = await personal_oauth_headers(store, owner, connection["id"], connection["endpoint"])
                if not await store.transition(owner, identity, "approved", "dispatching"):
                    raise PersonalMcpError("personal_mcp_confirmation_unavailable")
                dispatched = True
                if oauth_headers is not None:
                    headers = oauth_headers
                    redact = [headers["Authorization"].removeprefix("Bearer ")]
                else:
                    credentials = store.unseal(owner, connection["id"], "credentials", connection["auth_cipher"])
                    headers = auth_headers(connection["auth_type"], credentials)
                    redact = [secret for secret in credentials.values() if secret and secret != credentials.get("header")]
                try:
                    result = await _call_remote(connection["endpoint"], headers, tool["remote_name"], args)
                except Exception as first:
                    if not is_upstream_auth_error(first):
                        raise
                    refused = True
                    if oauth_headers is None:
                        raise
                    # A remembered access token can be refused before it expires (for example when the
                    # service revoked Bowhead's access). Renew once with that token marked as used: an
                    # ended sign-in or an unanswered renewal is reported as "not run".
                    refused_token = oauth_headers["Authorization"].removeprefix("Bearer ")
                    oauth_headers = await personal_oauth_headers(store, owner, connection["id"], connection["endpoint"], rejected_token=refused_token)
                    headers = oauth_headers
                    redact = [headers["Authorization"].removeprefix("Bearer ")]
                    refused = False
                    result = await _call_remote(connection["endpoint"], headers, tool["remote_name"], args)
                if result.is_error:
                    raise PersonalMcpError("personal_mcp_remote_error")
                data = result.structured_content
                if data is None:
                    data = "\n".join(block.text for block in result.content if getattr(block, "type", "") == "text")
                if len(canonical(data).encode()) > MAX_RESULT_BYTES:
                    raise PersonalMcpError("personal_mcp_result_limit")
                # Never reflect supplied credentials even if the remote echoes them.
                safe_blob = canonical(_safe(data))
                for secret in redact:
                    safe_blob = safe_blob.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[redacted]")
                await store.transition(owner, identity, "dispatching", "ok", duration=int((time.perf_counter() - started) * 1000))
                return {"ok": True, "data": json.loads(safe_blob)}
            except BaseException as exc:
                if dispatched and isinstance(exc, Exception) and is_upstream_auth_error(exc):
                    refused = True
                sign_in = _sign_in_failure(exc)
                if dispatched and oauth_headers is not None and isinstance(exc, Exception) and is_upstream_auth_error(exc):
                    # The service refused the access token: never offer it again.
                    from bowhead_core.mcp_oauth import oauth_broker

                    oauth_broker(store).invalidate(_grant_key(owner, tool["connection_id"]), oauth_headers["Authorization"].removeprefix("Bearer "))
                if dispatched and sign_in is None and isinstance(exc, Exception) and not isinstance(exc, PersonalMcpError):
                    # A renewal that ended the sign-in already marked the connection; one that went
                    # unanswered keeps it. Only a refused or failed dispatch is recorded here.
                    with anyio.CancelScope(shield=True):
                        try:
                            await store.connection_failure(
                                owner,
                                tool["connection_id"],
                                tool["connection_version"],
                                "needs_reauth" if is_upstream_auth_error(exc) else "unreachable",
                            )
                        except sqlite3.OperationalError as busy:
                            # A spent lease wait must not replace the call's own outcome.
                            if not is_authority_busy(busy):
                                raise
                            log.warning("personal_mcp.connection_failure_unrecorded", connection_id=tool["connection_id"])
                not_run = refused or sign_in is not None
                if identity:
                    with anyio.CancelScope(shield=True):
                        await self_finish(identity, dispatched and not not_run, started)
                if not isinstance(exc, Exception):
                    raise
                if card_open and identity:
                    await resolve_confirmation(call_ctx, confirmation_id=identity, outcome="unavailable")
                if sign_in is not None:
                    return _sign_in_result(tool["connection_name"], sign_in)
                if refused:
                    # Same answer as a later call to the connection the refusal marked for repair.
                    return _needs_sign_in_result(tool["connection_name"], auth_type)
                code = str(exc) if isinstance(exc, PersonalMcpError) else "personal_mcp_call_failed"
                return {
                    "ok": False,
                    "error_kind": "personal_mcp_outcome_unknown" if dispatched else code,
                    "error": "本次外部操作结果未知，请到服务中核实；不要自动重试。"
                    if dispatched
                    else "本次个人工具调用未执行。请检查确认状态，或到个人设置的我的连接修复；不要自动重试。",
                }

        async def self_finish(identity: str, dispatched: bool, started: float) -> None:
            if dispatched:
                await store.transition(owner, identity, "dispatching", "unknown", duration=int((time.perf_counter() - started) * 1000))
            else:
                await store.transition(owner, identity, "pending", "denied")
                await store.transition(owner, identity, "approved", "denied")
                await store.transition(owner, identity, "dispatching", "denied")

        return handler

    for tool in await store.tools_needing_sign_in(owner, binding):
        specs.append(_needs_sign_in_spec(tool))
    for tool in tools:
        specs.append(
            ToolSpec(
                schema=ToolSchema(
                    name=f"personalmcp.{tool['connection_id']}.{tool['id']}",
                    description=f"{tool['connection_name']} · {_safe(tool['remote_name'])}: {tool['description']}\nEvery call requires the owner's confirmation; never retry a failed action.",
                    input_schema=json.loads(tool["schema_json"]),
                ),
                handler=make_handler(tool),
                source="external",
                personal_mcp_label=f"{tool['connection_name']} · {_safe(tool['remote_name'])}",
                allowed_for_roles=frozenset({"analyst"}),
            )
        )
    return specs

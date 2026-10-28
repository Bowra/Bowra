"""Deterministic safety boundary for consumer-facing Agent listings.

The scanner never returns matched text.  Reports are keyed by the exact
normalized listing hash so a later edit cannot inherit an earlier verdict.
Runtime prompt/tool safety remains a separate concern.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.scrubber import scrub_with_count
from bowhead_core.secret_redaction import extract_secrets

LISTING_SAFETY_SCANNER_VERSION = "listing-safety-det-v1"
NO_TEAM_DICTIONARY_REVISION = "none"

BLOCKED_LISTING_DISPLAY_NAME = "Listing needs review"
BLOCKED_LISTING_SUMMARY = "Listing content is hidden until its owner resolves the safety findings."

_FORBIDDEN_URL_SCHEME_RE = re.compile(r"(?i)(?<![a-z0-9+.-])(?:javascript|data|vbscript|file)\s*:")
_URL_USERINFO_RE = re.compile(r"(?i)https?://[^\s/?#@]+@")
_BIDI_CONTROL_CHARACTERS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    },
)
_SCRUBBER_RULE_FAMILIES = {
    "身份证": "pii_cn_national_id",
    "手机号": "pii_cn_mobile",
    "邮箱": "pii_email",
    "api_key": "credential_or_secret",
}


@dataclass(frozen=True, slots=True)
class ListingSafetyFinding:
    rule_family: str
    field_path: str
    severity: str = "critical"


@dataclass(frozen=True, slots=True)
class ListingSafetyResult:
    listing_hash: str
    scanner_version: str
    deterministic_status: str
    semantic_status: str
    effective_status: str
    team_dictionary_revision: str
    findings: tuple[ListingSafetyFinding, ...]
    normalized_description: str
    routing_safety_reason: str | None

    @property
    def blocked(self) -> bool:
        return self.effective_status == "blocked"

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(finding.rule_family for finding in self.findings))


@dataclass(frozen=True, slots=True)
class PersistedListingSafetyReport:
    id: str
    team_id: str
    agent_id: str
    listing_hash: str
    scanner_version: str
    deterministic_status: str
    semantic_status: str
    effective_status: str
    team_dictionary_revision: str
    created_at: str
    completed_at: str
    findings: tuple[ListingSafetyFinding, ...]


@dataclass(frozen=True, slots=True)
class ListingSafetyInventoryBatch:
    scanned: int
    blocked: int
    persisted: int
    next_cursor: str | None
    complete: bool


def _canonical_field(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or ""))


def _has_forbidden_control(value: str) -> bool:
    return any(
        (unicodedata.category(character) == "Cc" and character not in "\n\r\t") or character in _BIDI_CONTROL_CHARACTERS for character in value
    )


def _listing_hash(fields: dict[str, str]) -> str:
    encoded = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def scan_agent_listing(
    *,
    name: object,
    display_name: object,
    description: object,
    team_dictionary_revision: str = NO_TEAM_DICTIONARY_REVISION,
) -> ListingSafetyResult:
    """Scan the current bounded listing fields without retaining matched text."""
    fields = {
        "description": _canonical_field(description),
        "display_name": _canonical_field(display_name),
        "name": _canonical_field(name),
    }
    findings: list[ListingSafetyFinding] = []

    for field_name, value in fields.items():
        field_path = f"listing.{field_name}"
        if _has_forbidden_control(value):
            findings.append(ListingSafetyFinding("forbidden_control_character", field_path))
        try:
            _redacted, secret_hits = extract_secrets(value)
        except Exception:
            findings.append(ListingSafetyFinding("listing_safety_check_failed", field_path))
        else:
            if secret_hits:
                findings.append(ListingSafetyFinding("credential_or_secret", field_path))
        try:
            _scrubbed, pii_counts = scrub_with_count(value)
        except Exception:
            findings.append(ListingSafetyFinding("listing_safety_check_failed", field_path))
        else:
            for pii_kind in sorted(pii_counts):
                findings.append(
                    ListingSafetyFinding(
                        _SCRUBBER_RULE_FAMILIES.get(pii_kind, "pii_unknown"),
                        field_path,
                    ),
                )
        if _FORBIDDEN_URL_SCHEME_RE.search(value) or _URL_USERINFO_RE.search(value):
            findings.append(ListingSafetyFinding("unsafe_url", field_path))

    from bowhead_core.agents.exceptions import ManifestSecurityError
    from bowhead_core.agents.manifest_security import lint_user_append
    from bowhead_core.agents.plan_admission import normalize_agent_routing_description

    for field_name in ("name", "display_name"):
        try:
            lint_user_append(fields[field_name])
        except ManifestSecurityError:
            findings.append(ListingSafetyFinding("listing_content_injection", f"listing.{field_name}"))
        except Exception:
            findings.append(ListingSafetyFinding("listing_safety_check_failed", f"listing.{field_name}"))

    try:
        normalized_description, routing_reason = normalize_agent_routing_description(fields["description"])
    except Exception:
        normalized_description = ""
        routing_reason = "listing_safety_check_failed"
    if routing_reason is not None:
        findings.append(ListingSafetyFinding(routing_reason, "listing.description"))

    unique_findings = tuple(ListingSafetyFinding(*key) for key in sorted({(item.rule_family, item.field_path, item.severity) for item in findings}))
    deterministic_status = "blocked" if unique_findings else "pass"
    return ListingSafetyResult(
        listing_hash=_listing_hash(fields),
        scanner_version=LISTING_SAFETY_SCANNER_VERSION,
        deterministic_status=deterministic_status,
        semantic_status="not_run",
        effective_status=deterministic_status,
        team_dictionary_revision=team_dictionary_revision,
        findings=unique_findings,
        normalized_description="" if unique_findings else normalized_description,
        routing_safety_reason=routing_reason,
    )


def _report_id(*, team_id: str, agent_id: str, result: ListingSafetyResult) -> str:
    payload = "\x00".join(
        (
            team_id,
            agent_id,
            result.listing_hash,
            result.scanner_version,
            result.team_dictionary_revision,
        ),
    ).encode()
    return f"alsr_{hashlib.sha256(payload).hexdigest()[:32]}"


def _encode_cursor(team_id: str, agent_id: str) -> str:
    payload = json.dumps([team_id, agent_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[str, str]:
    if not cursor:
        return "", ""
    try:
        payload = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        parsed = json.loads(payload)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid listing safety inventory cursor") from exc
    if not isinstance(parsed, list) or len(parsed) != 2 or not all(isinstance(value, str) for value in parsed):
        raise ValueError("invalid listing safety inventory cursor")
    return parsed[0], parsed[1]


class AgentListingSafetyStore:
    """Persist and inventory exact-hash listing safety reports."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    async def persist(
        self,
        *,
        team_id: str,
        agent_id: str,
        result: ListingSafetyResult,
    ) -> PersistedListingSafetyReport:
        report_id = _report_id(team_id=team_id, agent_id=agent_id, result=result)
        timestamp = datetime.now(UTC).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            await self._persist_conn(
                db,
                report_id=report_id,
                team_id=team_id,
                agent_id=agent_id,
                result=result,
                timestamp=timestamp,
            )
            if result.blocked:
                await self._sanitize_projection_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    result=result,
                    timestamp=timestamp,
                )
            await db.commit()
        report = await self.get(report_id=report_id, team_id=team_id, agent_id=agent_id)
        if report is None:  # pragma: no cover - a committed insert/select mismatch is storage corruption
            raise RuntimeError("listing safety report persistence failed")
        return report

    async def persist_many(
        self,
        *,
        rows: tuple[tuple[str, str, ListingSafetyResult], ...],
    ) -> int:
        if not rows:
            return 0
        timestamp = datetime.now(UTC).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            before = db.total_changes
            for team_id, agent_id, result in rows:
                await self._persist_conn(
                    db,
                    report_id=_report_id(team_id=team_id, agent_id=agent_id, result=result),
                    team_id=team_id,
                    agent_id=agent_id,
                    result=result,
                    timestamp=timestamp,
                )
                if result.blocked:
                    await self._sanitize_projection_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        result=result,
                        timestamp=timestamp,
                    )
            inserted = db.total_changes - before
            await db.commit()
        return inserted

    @staticmethod
    async def _persist_conn(
        db: aiosqlite.Connection,
        *,
        report_id: str,
        team_id: str,
        agent_id: str,
        result: ListingSafetyResult,
        timestamp: str,
    ) -> None:
        await db.execute(
            """
            INSERT INTO agent_listing_safety_report(
              id,team_id,agent_id,listing_hash,scanner_version,deterministic_status,
              semantic_status,effective_status,team_dictionary_revision,created_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(team_id,agent_id,listing_hash,scanner_version,team_dictionary_revision) DO NOTHING
            """,
            (
                report_id,
                team_id,
                agent_id,
                result.listing_hash,
                result.scanner_version,
                result.deterministic_status,
                result.semantic_status,
                result.effective_status,
                result.team_dictionary_revision,
                timestamp,
                timestamp,
            ),
        )
        for ordinal, finding in enumerate(result.findings):
            await db.execute(
                """
                INSERT INTO agent_listing_safety_finding(
                  report_id,ordinal,rule_family,field_path,severity,disposition
                ) VALUES (?,?,?,?,?,'open')
                ON CONFLICT(report_id,ordinal) DO NOTHING
                """,
                (
                    report_id,
                    ordinal,
                    finding.rule_family,
                    finding.field_path,
                    finding.severity,
                ),
            )

    @staticmethod
    async def _sanitize_projection_conn(
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        result: ListingSafetyResult,
        timestamp: str,
    ) -> None:
        """Remove legacy listing copy from a persisted consumer projection."""
        async with db.execute(
            """
            SELECT canonical_name,canonical_summary,safety_summary_json,search_text
            FROM agent_catalog_projection WHERE team_id=? AND agent_id=?
            """,
            (team_id, agent_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return
        try:
            current_safety = json.loads(str(row[2]))
        except (json.JSONDecodeError, TypeError, ValueError):
            current_safety = None
        already_sanitized = (
            str(row[0]) == BLOCKED_LISTING_DISPLAY_NAME
            and str(row[1]) == BLOCKED_LISTING_SUMMARY
            and str(row[3]) == ""
            and isinstance(current_safety, dict)
            and current_safety.get("listing_status") == "blocked"
            and current_safety.get("listing_hash") == result.listing_hash
        )
        if already_sanitized:
            return
        safety_summary = json.dumps(
            {
                "status": "fail",
                "reasons": ["listing_safety_blocked"],
                "listing_status": "blocked",
                "listing_reasons": result.reason_codes,
                "listing_hash": result.listing_hash,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        projection_hash = hashlib.sha256(
            f"blocked\0{result.listing_hash}\0{result.scanner_version}".encode(),
        ).hexdigest()
        await db.execute(
            """
            UPDATE agent_catalog_projection
            SET canonical_name=?,canonical_summary=?,safety_summary_json=?,search_text='',
                projection_hash=?,built_at=?
            WHERE team_id=? AND agent_id=?
            """,
            (
                BLOCKED_LISTING_DISPLAY_NAME,
                BLOCKED_LISTING_SUMMARY,
                safety_summary,
                projection_hash,
                timestamp,
                team_id,
                agent_id,
            ),
        )

    async def get(
        self,
        *,
        report_id: str,
        team_id: str,
        agent_id: str,
    ) -> PersistedListingSafetyReport | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id,team_id,agent_id,listing_hash,scanner_version,deterministic_status,
                       semantic_status,effective_status,team_dictionary_revision,created_at,completed_at
                FROM agent_listing_safety_report
                WHERE id=? AND team_id=? AND agent_id=?
                """,
                (report_id, team_id, agent_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            async with db.execute(
                """
                SELECT rule_family,field_path,severity
                FROM agent_listing_safety_finding
                WHERE report_id=? ORDER BY ordinal
                """,
                (report_id,),
            ) as cursor:
                finding_rows = await cursor.fetchall()
        return PersistedListingSafetyReport(
            id=str(row["id"]),
            team_id=str(row["team_id"]),
            agent_id=str(row["agent_id"]),
            listing_hash=str(row["listing_hash"]),
            scanner_version=str(row["scanner_version"]),
            deterministic_status=str(row["deterministic_status"]),
            semantic_status=str(row["semantic_status"]),
            effective_status=str(row["effective_status"]),
            team_dictionary_revision=str(row["team_dictionary_revision"]),
            created_at=str(row["created_at"]),
            completed_at=str(row["completed_at"]),
            findings=tuple(
                ListingSafetyFinding(
                    rule_family=str(finding["rule_family"]),
                    field_path=str(finding["field_path"]),
                    severity=str(finding["severity"]),
                )
                for finding in finding_rows
            ),
        )

    async def scan_current_agent(self, *, team_id: str, agent_id: str, persist: bool = True) -> ListingSafetyResult | None:
        """Scan the Agent's current listing text.

        Execution callers keep the default: a blocked result is persisted so the Owner sees it.
        ``persist=False`` is for read-only explanation of the same decision; it must never write.
        """
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT name,display_name,description FROM team_subagent WHERE team_id=? AND id=?",
                (team_id, agent_id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return None
        result = scan_agent_listing(name=row[0], display_name=row[1], description=row[2])
        if result.blocked and persist:
            await self.persist(team_id=team_id, agent_id=agent_id, result=result)
        return result

    async def scan_inventory(
        self,
        *,
        apply: bool,
        batch_size: int = 100,
        cursor: str | None = None,
    ) -> ListingSafetyInventoryBatch:
        if batch_size < 1 or batch_size > 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        cursor_team_id, cursor_agent_id = _decode_cursor(cursor)
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT team_id,id,name,display_name,description
                FROM team_subagent
                WHERE team_id>? OR (team_id=? AND id>?)
                ORDER BY team_id,id
                LIMIT ?
                """,
                (cursor_team_id, cursor_team_id, cursor_agent_id, batch_size + 1),
            ) as query,
        ):
            rows = await query.fetchall()
        selected = rows[:batch_size]
        scanned = tuple(
            (
                str(row[0]),
                str(row[1]),
                scan_agent_listing(name=row[2], display_name=row[3], description=row[4]),
            )
            for row in selected
        )
        persisted = await self.persist_many(rows=scanned) if apply else 0
        complete = len(rows) <= batch_size
        next_cursor = None
        if not complete and selected:
            next_cursor = _encode_cursor(str(selected[-1][0]), str(selected[-1][1]))
        return ListingSafetyInventoryBatch(
            scanned=len(scanned),
            blocked=sum(result.blocked for _team_id, _agent_id, result in scanned),
            persisted=persisted,
            next_cursor=next_cursor,
            complete=complete,
        )


__all__ = [
    "AgentListingSafetyStore",
    "BLOCKED_LISTING_DISPLAY_NAME",
    "BLOCKED_LISTING_SUMMARY",
    "LISTING_SAFETY_SCANNER_VERSION",
    "ListingSafetyFinding",
    "ListingSafetyInventoryBatch",
    "ListingSafetyResult",
    "PersistedListingSafetyReport",
    "scan_agent_listing",
]

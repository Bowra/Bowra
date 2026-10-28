"""Transactional effective-name reservations for Agent release candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bowhead_core.team_subagent_store import TeamSubagentNameConflictError, effective_agent_name_key

if TYPE_CHECKING:
    from collections.abc import Mapping

    import aiosqlite


class AgentCandidateNameConflictError(ValueError):
    """A candidate requested a name already reserved in the team."""


class AgentCandidateNameStateError(RuntimeError):
    """Candidate state and durable name-claim state disagree."""


@dataclass(frozen=True, slots=True)
class AgentCandidateNameClaim:
    locale: str
    normalized_key: str
    claim_kind: str
    state: str


class AgentCandidateNameStore:
    """Connection-scoped primitives used by the M2 publication transaction.

    These methods deliberately do not begin or commit transactions. Candidate
    state, revision freeze, name claims and activation must share one caller-
    owned ``BEGIN IMMEDIATE`` transaction.
    """

    async def claim_agent_names_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        names_by_locale: Mapping[str, str],
        timestamp: str,
    ) -> None:
        """Reclaim a released live name set without manufacturing a candidate."""
        requested = self._normalize_names(names_by_locale)
        for locale, normalized_key in requested:
            rows = await self._effective_claim_rows_conn(
                db,
                team_id=team_id,
                locale=locale,
                normalized_key=normalized_key,
            )
            if any(
                not (holder_kind == "agent" and claimed_agent_id == agent_id) for _claimed_locale, holder_kind, _holder_id, claimed_agent_id in rows
            ):
                raise AgentCandidateNameConflictError(
                    "display name is unavailable in this team",
                )
        target_keys = set(requested)
        await self._release_superseded_agent_names_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            target_keys=target_keys,
            timestamp=timestamp,
        )
        for locale, normalized_key in requested:
            await db.execute(
                """
                INSERT OR IGNORE INTO agent_name_claim(
                  team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
                ) VALUES (?, ?, ?, 'agent', ?, ?, ?)
                """,
                (team_id, locale, normalized_key, agent_id, agent_id, timestamp),
            )

    async def release_agent_names_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        timestamp: str,
    ) -> None:
        """Release all live names for an archived Agent."""
        await self._release_superseded_agent_names_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            target_keys=set(),
            timestamp=timestamp,
        )

    async def reserve_names_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        names_by_locale: Mapping[str, str],
        timestamp: str,
    ) -> tuple[AgentCandidateNameClaim, ...]:
        candidate_state = await self._candidate_state_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
        )
        if candidate_state not in {"prepared", "pending"}:
            raise AgentCandidateNameStateError("candidate must be active to reserve names")
        requested = self._normalize_names(names_by_locale)
        existing = await self._candidate_claims_conn(db, candidate_id=candidate_id)
        if existing:
            if tuple((item.locale, item.normalized_key) for item in existing) != tuple((locale, key) for locale, key in requested) or any(
                item.state != "reserved" for item in existing
            ):
                raise AgentCandidateNameStateError("candidate name reservation is immutable")
            await self._validate_claim_owners_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                candidate_id=candidate_id,
                claims=existing,
                expected_state="reserved",
            )
            for item in existing:
                rows = await self._effective_claim_rows_conn(
                    db,
                    team_id=team_id,
                    locale=item.locale,
                    normalized_key=item.normalized_key,
                )
                if any(
                    not (
                        (holder_kind == "agent" and claimed_agent_id == agent_id)
                        or (holder_kind == "candidate" and holder_id == candidate_id and claimed_agent_id == agent_id)
                    )
                    for _claimed_locale, holder_kind, holder_id, claimed_agent_id in rows
                ):
                    raise AgentCandidateNameConflictError(
                        "display name is unavailable in this team",
                    )
            return existing

        claims: list[AgentCandidateNameClaim] = []
        for locale, normalized_key in requested:
            rows = await self._effective_claim_rows_conn(
                db,
                team_id=team_id,
                locale=locale,
                normalized_key=normalized_key,
            )
            if any(
                not (
                    (holder_kind == "agent" and claimed_agent_id == agent_id)
                    or (holder_kind == "candidate" and holder_id == candidate_id and claimed_agent_id == agent_id)
                )
                for _claimed_locale, holder_kind, holder_id, claimed_agent_id in rows
            ):
                raise AgentCandidateNameConflictError("display name is unavailable in this team")
            exact = next((row for row in rows if row[0] == locale), None)
            if exact is None:
                claim_kind = "exclusive"
                await db.execute(
                    """
                    INSERT INTO agent_name_claim(
                      team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
                    ) VALUES (?,?,?,'candidate',?,?,?)
                    """,
                    (team_id, locale, normalized_key, candidate_id, agent_id, timestamp),
                )
            elif exact[1] == "agent" and exact[3] == agent_id:
                claim_kind = "inherited"
            elif exact[1] == "candidate" and exact[2] == candidate_id and exact[3] == agent_id:
                claim_kind = "exclusive"
            else:
                raise AgentCandidateNameConflictError("display name is unavailable in this team")
            await db.execute(
                """
                INSERT INTO agent_release_candidate_name_claim(
                  team_id,candidate_id,agent_id,locale,normalized_key,
                  claim_kind,state,claimed_at,settled_at
                ) VALUES (?,?,?,?,?,?,'reserved',?,NULL)
                """,
                (team_id, candidate_id, agent_id, locale, normalized_key, claim_kind, timestamp),
            )
            claims.append(
                AgentCandidateNameClaim(
                    locale=locale,
                    normalized_key=normalized_key,
                    claim_kind=claim_kind,
                    state="reserved",
                ),
            )
        return tuple(claims)

    async def release_names_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        timestamp: str,
    ) -> tuple[AgentCandidateNameClaim, ...]:
        candidate_state = await self._candidate_state_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
        )
        if candidate_state not in {"rejected", "withdrawn", "stale"}:
            raise AgentCandidateNameStateError("candidate must be terminal to release names")
        claims = await self._candidate_claims_conn(db, candidate_id=candidate_id)
        if not claims:
            raise AgentCandidateNameStateError("candidate has no durable name reservation")
        if all(item.state == "released" for item in claims):
            await self._validate_claim_owners_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                candidate_id=candidate_id,
                claims=claims,
                expected_state="released",
            )
            return claims
        if any(item.state != "reserved" for item in claims):
            raise AgentCandidateNameStateError("candidate name reservation has mixed state")
        await self._validate_claim_owners_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
            claims=claims,
            expected_state="reserved",
        )
        for item in claims:
            if item.claim_kind == "exclusive":
                deleted = await db.execute(
                    """
                    DELETE FROM agent_name_claim
                    WHERE team_id=? AND locale=? AND normalized_key=?
                      AND holder_kind='candidate' AND holder_id=? AND agent_id=?
                    """,
                    (team_id, item.locale, item.normalized_key, candidate_id, agent_id),
                )
                if deleted.rowcount != 1:
                    raise AgentCandidateNameStateError("reserved candidate name changed owner")
        await db.execute(
            """
            UPDATE agent_release_candidate_name_claim
            SET state='released',settled_at=?
            WHERE candidate_id=? AND state='reserved'
            """,
            (timestamp, candidate_id),
        )
        return tuple(
            AgentCandidateNameClaim(
                locale=item.locale,
                normalized_key=item.normalized_key,
                claim_kind=item.claim_kind,
                state="released",
            )
            for item in claims
        )

    async def activate_names_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        timestamp: str,
    ) -> tuple[AgentCandidateNameClaim, ...]:
        candidate_state = await self._candidate_state_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
        )
        if candidate_state not in {"approved", "activated"}:
            raise AgentCandidateNameStateError("candidate cannot activate names from its current state")
        claims = await self._candidate_claims_conn(db, candidate_id=candidate_id)
        if not claims:
            raise AgentCandidateNameStateError("candidate has no durable name reservation")
        if all(item.state == "activated" for item in claims):
            await self._validate_claim_owners_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                candidate_id=candidate_id,
                claims=claims,
                expected_state="activated",
            )
            return claims
        if candidate_state == "activated":
            raise AgentCandidateNameStateError("activated candidate has unsettled name reservations")
        if any(item.state != "reserved" for item in claims):
            raise AgentCandidateNameStateError("candidate name reservation has mixed state")
        await self._validate_claim_owners_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
            claims=claims,
            expected_state="reserved",
        )

        target_keys = {(item.locale, item.normalized_key) for item in claims}
        await self._release_superseded_agent_names_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            target_keys=target_keys,
            timestamp=timestamp,
        )
        for item in claims:
            async with db.execute(
                """
                SELECT holder_kind,holder_id,agent_id
                FROM agent_name_claim
                WHERE team_id=? AND locale=? AND normalized_key=?
                """,
                (team_id, item.locale, item.normalized_key),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise AgentCandidateNameStateError("reserved candidate name is missing")
            if item.claim_kind == "exclusive":
                if not (str(row[0]) == "candidate" and str(row[1]) == candidate_id and str(row[2] or "") == agent_id):
                    raise AgentCandidateNameStateError("reserved candidate name changed owner")
                updated = await db.execute(
                    """
                    UPDATE agent_name_claim
                    SET holder_kind='agent',holder_id=?,agent_id=?,claimed_at=?
                    WHERE team_id=? AND locale=? AND normalized_key=?
                      AND holder_kind='candidate' AND holder_id=? AND agent_id=?
                    """,
                    (
                        agent_id,
                        agent_id,
                        timestamp,
                        team_id,
                        item.locale,
                        item.normalized_key,
                        candidate_id,
                        agent_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentCandidateNameStateError("reserved candidate name changed owner")
            elif not (str(row[0]) == "agent" and str(row[2] or "") == agent_id):
                raise AgentCandidateNameStateError("inherited Agent name changed owner")
        await db.execute(
            """
            UPDATE agent_release_candidate_name_claim
            SET state='activated',settled_at=?
            WHERE candidate_id=? AND state='reserved'
            """,
            (timestamp, candidate_id),
        )
        return tuple(
            AgentCandidateNameClaim(
                locale=item.locale,
                normalized_key=item.normalized_key,
                claim_kind=item.claim_kind,
                state="activated",
            )
            for item in claims
        )

    async def _validate_claim_owners_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        claims: tuple[AgentCandidateNameClaim, ...],
        expected_state: str,
    ) -> None:
        for item in claims:
            async with db.execute(
                """
                SELECT holder_kind,holder_id,agent_id FROM agent_name_claim
                WHERE team_id=? AND locale=? AND normalized_key=?
                """,
                (team_id, item.locale, item.normalized_key),
            ) as cursor:
                row = await cursor.fetchone()
            if expected_state == "released":
                if item.claim_kind == "exclusive" and row is not None and str(row[0]) == "candidate" and str(row[1]) == candidate_id:
                    raise AgentCandidateNameStateError("released candidate still owns a name")
                continue
            if row is None:
                raise AgentCandidateNameStateError("candidate name claim is missing")
            if expected_state == "activated":
                valid = str(row[0]) == "agent" and str(row[2] or "") == agent_id
            elif item.claim_kind == "exclusive":
                valid = str(row[0]) == "candidate" and str(row[1]) == candidate_id and str(row[2] or "") == agent_id
            else:
                valid = str(row[0]) == "agent" and str(row[2] or "") == agent_id
            if not valid:
                raise AgentCandidateNameStateError("candidate name claim changed owner")

    async def _release_superseded_agent_names_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        target_keys: set[tuple[str, str]],
        timestamp: str,
    ) -> None:
        async with db.execute(
            """
            SELECT locale,normalized_key
            FROM agent_name_claim
            WHERE team_id=? AND holder_kind='agent' AND agent_id=?
            """,
            (team_id, agent_id),
        ) as cursor:
            direct_keys = {(str(row[0]), str(row[1])) for row in await cursor.fetchall()}
        for locale, normalized_key in direct_keys - target_keys:
            await db.execute(
                """
                DELETE FROM agent_name_claim
                WHERE team_id=? AND locale=? AND normalized_key=?
                  AND holder_kind='agent' AND agent_id=?
                """,
                (team_id, locale, normalized_key, agent_id),
            )

        async with db.execute(
            """
            SELECT normalized_key
            FROM agent_name_conflict_group
            WHERE team_id=? AND agent_id=? AND resolved_at IS NULL
            """,
            (team_id, agent_id),
        ) as cursor:
            conflict_keys = {str(row[0]) for row in await cursor.fetchall()}
        for normalized_key in conflict_keys:
            if ("*", normalized_key) in target_keys:
                continue
            await db.execute(
                """
                UPDATE agent_name_conflict_group SET resolved_at=?
                WHERE team_id=? AND normalized_key=? AND agent_id=? AND resolved_at IS NULL
                """,
                (timestamp, team_id, normalized_key, agent_id),
            )
            async with db.execute(
                """
                SELECT agent_id FROM agent_name_conflict_group
                WHERE team_id=? AND normalized_key=? AND resolved_at IS NULL
                ORDER BY agent_id
                """,
                (team_id, normalized_key),
            ) as cursor:
                remaining = [str(row[0]) for row in await cursor.fetchall()]
            if len(remaining) > 1:
                continue
            async with db.execute(
                """
                SELECT holder_kind FROM agent_name_claim
                WHERE team_id=? AND locale='*' AND normalized_key=?
                """,
                (team_id, normalized_key),
            ) as cursor:
                current_claim = await cursor.fetchone()
            if current_claim is None or str(current_claim[0]) != "legacy_conflict":
                continue
            await db.execute(
                """
                DELETE FROM agent_name_claim
                WHERE team_id=? AND locale='*' AND normalized_key=?
                  AND holder_kind='legacy_conflict'
                """,
                (team_id, normalized_key),
            )
            if remaining:
                await db.execute(
                    """
                    INSERT INTO agent_name_claim(
                      team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
                    ) VALUES (?,'*',?,'agent',?,?,?)
                    """,
                    (team_id, normalized_key, remaining[0], remaining[0], timestamp),
                )

    async def _candidate_state_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
    ) -> str:
        async with db.execute(
            """
            SELECT state FROM team_subagent_release_candidate
            WHERE team_id=? AND subagent_id=? AND id=?
            """,
            (team_id, agent_id, candidate_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentCandidateNameStateError("release candidate does not exist")
        return str(row[0])

    async def _candidate_claims_conn(
        self,
        db: aiosqlite.Connection,
        *,
        candidate_id: str,
    ) -> tuple[AgentCandidateNameClaim, ...]:
        async with db.execute(
            """
            SELECT locale,normalized_key,claim_kind,state
            FROM agent_release_candidate_name_claim
            WHERE candidate_id=?
            ORDER BY locale,normalized_key
            """,
            (candidate_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(
            AgentCandidateNameClaim(
                locale=str(row[0]),
                normalized_key=str(row[1]),
                claim_kind=str(row[2]),
                state=str(row[3]),
            )
            for row in rows
        )

    async def _effective_claim_rows_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        locale: str,
        normalized_key: str,
    ) -> tuple[tuple[str, str, str, str], ...]:
        language = locale.split("-", 1)[0]
        locale_clause = "1=1" if locale == "*" else "(locale='*' OR locale=? OR locale=? OR locale LIKE ?)"
        parameters: tuple[str, ...] = (team_id, normalized_key) if locale == "*" else (team_id, normalized_key, locale, language, f"{language}-%")
        async with db.execute(
            f"""
            SELECT locale,holder_kind,holder_id,COALESCE(agent_id,'')
            FROM agent_name_claim
            WHERE team_id=? AND normalized_key=? AND {locale_clause}
            ORDER BY locale
            """,  # noqa: S608 -- locale_clause is a closed internal constant.
            parameters,
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple((str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows)

    def _normalize_names(self, names_by_locale: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        if not names_by_locale:
            raise ValueError("candidate must reserve at least one display name")
        normalized: dict[str, str] = {}
        for raw_locale, display_name in names_by_locale.items():
            locale = str(raw_locale).strip().lower()
            if not locale:
                raise ValueError("candidate name locale must not be empty")
            try:
                key = effective_agent_name_key(display_name)
            except TeamSubagentNameConflictError as exc:
                raise AgentCandidateNameConflictError("display name is unavailable in this team") from exc
            if locale in normalized:
                raise ValueError("candidate has duplicate normalized locale")
            normalized[locale] = key
        return tuple(sorted(normalized.items()))


__all__ = [
    "AgentCandidateNameClaim",
    "AgentCandidateNameConflictError",
    "AgentCandidateNameStateError",
    "AgentCandidateNameStore",
]

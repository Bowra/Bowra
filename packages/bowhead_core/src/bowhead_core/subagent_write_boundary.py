"""Release A write boundary for the legacy ``team_subagent`` aggregate.

The capability token in this module is an architectural guard, not an
authentication credential.  It prevents generic grant repositories from
accidentally mutating Subagent state outside ``TeamSubagentStore`` while the
Release A compatibility layer and legacy readers coexist.
"""

from __future__ import annotations

from dataclasses import dataclass

TEAM_SUBAGENT_RESOURCE_KIND = "team_subagent"


class TeamSubagentWriteBoundaryError(ValueError):
    """A legacy generic API attempted to mutate a Subagent aggregate."""

    code = "subagent_write_api_obsolete"

    def __init__(self) -> None:
        super().__init__(
            "team_subagent writes must use the Subagent management aggregate API",
        )


@dataclass(frozen=True, slots=True)
class SubagentTransactionToken:
    """Opaque same-process capability accepted by generic grant primitives."""

    _nonce: object


_TOKEN_NONCE = object()
_TRANSACTION_TOKEN = SubagentTransactionToken(_TOKEN_NONCE)


def _issue_team_subagent_transaction_token() -> SubagentTransactionToken:
    """Return the singleton capability reserved for ``TeamSubagentStore``."""
    return _TRANSACTION_TOKEN


def require_team_subagent_transaction_token(
    resource_kind: str,
    token: SubagentTransactionToken | None,
) -> None:
    """Reject Subagent writes that are not owned by the aggregate transaction."""
    if resource_kind == TEAM_SUBAGENT_RESOURCE_KIND and token is not _TRANSACTION_TOKEN:
        raise TeamSubagentWriteBoundaryError()


def reject_obsolete_team_subagent_write(resource_kind: str) -> None:
    """Reject a generic share/grant mutation for a Subagent resource."""
    if resource_kind == TEAM_SUBAGENT_RESOURCE_KIND:
        raise TeamSubagentWriteBoundaryError()

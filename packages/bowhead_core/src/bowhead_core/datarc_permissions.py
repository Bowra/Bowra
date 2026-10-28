"""Filter Datarc terms by the asking member's permissions.

Datarc recall (``/keywords/recall/``), the schema vector collection and the
measure/dimension listings return workspace-wide terms with no member context. A
member without column or row permission would otherwise see those terms in
schema listings, prompt hints and clarification options. Every tool that hands
such terms onward checks them here, in one batched ``/tokens/permissions/`` call.

Datarc still enforces permissions when a query runs, so an unavailable check
keeps the terms (as before this filter existed) and logs the failure instead of
emptying the workspace for everyone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

Decision = Literal["allowed", "denied", "unchecked"]


@dataclass(frozen=True)
class PermissionToken:
    word: str
    prop: str
    tuids: tuple[str, ...] = ()

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"word": self.word, "prop": self.prop}
        if self.tuids:
            out["tuids"] = list(self.tuids)
        return out


def split_tuids(raw: Any) -> tuple[str, ...]:
    """Recall returns model uids comma-joined; the permission check needs a list."""
    if isinstance(raw, (list, tuple)):
        return tuple(str(t) for t in raw if t)
    return tuple(t for t in str(raw or "").split(",") if t)


def _classify(status: str) -> Decision:
    s = status.strip().lower()
    if s in ("normal", "正常"):
        return "allowed"
    if s in ("no permission", "没有权限"):
        return "denied"
    # Invalid-token messages (unknown prop, field missing in this workspace) and
    # polysemy summaries are not a verdict about this member.
    return "unchecked"


async def check_tokens(datarc: Any, datarc_workspace_id: str, member_uid: str, tokens: list[PermissionToken]) -> list[Decision]:
    """One decision per token, in order, from one Datarc request.

    Decisions are not cached: the call takes tens of milliseconds, and a permission
    an administrator revokes in Datarc must take effect on the member's next turn.
    """
    if not tokens:
        return []
    check = getattr(datarc, "check_token_permissions", None)
    if not member_uid or not datarc_workspace_id or not callable(check):
        return ["unchecked"] * len(tokens)
    # Tokens without a prop cannot be checked; repeated (prop, tuids) pairs share one entry.
    batch = list(dict.fromkeys(t for t in tokens if t.prop))
    if not batch:
        return ["unchecked"] * len(tokens)
    try:
        statuses = await check(str(datarc_workspace_id), member_uid, [t.body() for t in batch])
        if not isinstance(statuses, list) or len(statuses) != len(batch):
            raise ValueError(f"expected {len(batch)} statuses, got {type(statuses).__name__}")
    except Exception as e:
        logger.warning(
            "datarc_permissions.check_failed ws=%s tokens=%d err=%s",
            datarc_workspace_id,
            len(batch),
            e,
        )
        _count("check_failed")
        return ["unchecked"] * len(tokens)
    by_token = {(t.prop, t.tuids): _classify(s) for t, s in zip(batch, statuses, strict=True)}
    out: list[Decision] = [by_token.get((t.prop, t.tuids), "unchecked") for t in tokens]
    denied = out.count("denied")
    if denied:
        _count("denied", denied)
    return out


def _count(outcome: str, n: int = 1) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.inc(
            "bowhead_datarc_token_permission_total",
            labels={"outcome": outcome},
            value=float(n),
            help="Datarc terms withheld for lack of member permission, and failed checks.",
        )
    except Exception:  # noqa: S110
        pass


def _first_name(item: Any) -> str:
    names = item.get("names") if isinstance(item, dict) else getattr(item, "names", None)
    return str((names or [""])[0] or "")


def _item_id(item: Any) -> Any:
    return item.get("id") if isinstance(item, dict) else getattr(item, "id", None)


async def filter_schema(
    datarc: Any,
    datarc_workspace_id: str,
    member_uid: str,
    measures: list[Any],
    dimensions: list[Any],
) -> tuple[list[Any], list[Any], int, int]:
    """Drop measures and dimensions the member has no column permission for.

    Returns ``(measures, dimensions, denied_measures, denied_dimensions)``.
    """

    def prop(kind: str, item: Any) -> str:
        item_id = _item_id(item)
        return f"{kind}{item_id}" if item_id not in (None, "") else ""

    tokens = [PermissionToken(_first_name(m), prop("m", m)) for m in measures]
    tokens += [PermissionToken(_first_name(d), prop("d", d)) for d in dimensions]
    decisions = await check_tokens(datarc, datarc_workspace_id, member_uid, tokens)
    m_dec, d_dec = decisions[: len(measures)], decisions[len(measures) :]
    kept_m = [m for m, d in zip(measures, m_dec, strict=True) if d != "denied"]
    kept_d = [x for x, d in zip(dimensions, d_dec, strict=True) if d != "denied"]
    return kept_m, kept_d, len(measures) - len(kept_m), len(dimensions) - len(kept_d)


def member_of(ctx: Any) -> str:
    return str(getattr(ctx, "member_uid", "") or "")

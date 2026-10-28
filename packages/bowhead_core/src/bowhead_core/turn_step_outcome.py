"""Terminal ``done.step_outcome``: how many recorded step issues were recovered, and whether an
unresolved one reached the answer.

Chat turn feedback signals D6 (``docs/design/chat-turn-feedback-signals-tech-design.md`` §3.3).

Every decision here reads event **structure** only (event type, step id, owning agent, tool name,
state, peer terminal status). It never reads previews, arguments or model prose.

- ``issue_steps``: steps whose last ``step_done`` state is ``failure`` or ``partial``. This is the
  set the frontend counted before this field existed, so old and new counts agree.
- An issue step is **recovered** when a later ``step_done`` with ``state == success`` exists for
  the same owner (the Lead is one owner) and the same tool, or when it belongs to a peer whose
  terminal is ``failed`` and a later dispatch of the same peer profile ended successfully.
  A step belongs to a peer either because the peer ran it, or because it is the Lead's spawn call
  for that peer (matched by ``call_id``). A spawn step is recovered only by the peer rule: a later
  spawn of a *different* peer is not a recovery of this one.
- ``unresolved_steps = issue_steps - recovered_steps + started_without_done``: a started step that
  never finished is not a success.
- ``answer_affected`` is true only for a completed turn whose unresolved issue belongs to a failed
  peer, is an unresolved ``partial`` batch, or belongs to a peer that ended with warnings. An
  unresolved Lead-owned failure does not count: the Lead saw the error before writing its answer,
  and the backend cannot prove dependency (Q3).

A missing ``step_outcome`` means unknown, never zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

STEP_OUTCOME_SCHEMA_VERSION = 1
#: Per-turn record cap. Exceeding it omits the field instead of publishing a partial count.
STEP_OUTCOME_RECORD_CAP = 2000

STEP_OUTCOME_EVENT_TYPES = frozenset({"step_started", "step_done", "lead.subagent_summary", "peer_dispatch_failed"})

_ISSUE_STATES = frozenset({"failure", "partial"})
_PEER_SUCCESS = frozenset({"succeeded", "succeeded_with_warnings"})


@dataclass(frozen=True, slots=True)
class StepFact:
    """One bounded structural record. It holds no preview, argument or prose."""

    seq: int
    event_type: str
    step_id: str | None = None
    #: Owning agent: the peer's subagent id, or ``None`` for the Lead.
    owner: str | None = None
    tool_name: str | None = None
    call_id: str | None = None
    state: str | None = None
    #: Peer terminal facts (``lead.subagent_summary`` / ``peer_dispatch_failed``).
    subagent_id: str | None = None
    profile: str | None = None
    peer_status: str | None = None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def step_fact_from_event(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    parent_agent_id: str | None,
    seq: int,
) -> StepFact | None:
    """Project one bus event onto a :class:`StepFact`, or ``None`` when it carries no usable fact."""

    if event_type in ("step_started", "step_done"):
        step_id = _text(payload.get("step_id"))
        if step_id is None:
            return None
        owner = _text(parent_agent_id) or _text(payload.get("parent_agent_id"))
        state = _text(payload.get("state")) if event_type == "step_done" else None
        return StepFact(
            seq=seq,
            event_type=event_type,
            step_id=step_id,
            owner=owner,
            tool_name=_text(payload.get("tool_name")),
            call_id=_text(payload.get("call_id")),
            state=state,
        )
    if event_type == "lead.subagent_summary":
        return StepFact(
            seq=seq,
            event_type=event_type,
            call_id=_text(payload.get("call_id")),
            subagent_id=_text(payload.get("subagent_id")) or _text(payload.get("spawn_id")),
            profile=_text(payload.get("profile")),
            peer_status=_text(payload.get("status")),
        )
    if event_type == "peer_dispatch_failed":
        return StepFact(
            seq=seq,
            event_type=event_type,
            call_id=_text(payload.get("call_id")),
            subagent_id=_text(payload.get("subagent_id")) or _text(payload.get("spawn_id")),
            profile=_text(payload.get("peer_name")),
            peer_status="failed",
        )
    return None


@dataclass(slots=True)
class _Peer:
    subagent_id: str
    profile: str | None
    status: str | None
    seq: int


def summarize_step_outcome(facts: Iterable[StepFact], *, status: str | None) -> dict[str, Any]:
    """Pure summary of one turn's recorded step facts (see the module docstring)."""

    ordered = sorted(facts, key=lambda fact: fact.seq)
    started: dict[str, StepFact] = {}
    last_done: dict[str, StepFact] = {}
    successes: list[StepFact] = []
    peers: dict[str, _Peer] = {}
    peer_by_call: dict[str, str] = {}
    for fact in ordered:
        if fact.event_type == "step_started" and fact.step_id is not None:
            started.setdefault(fact.step_id, fact)
        elif fact.event_type == "step_done" and fact.step_id is not None:
            last_done[fact.step_id] = fact
            if fact.state == "success":
                successes.append(fact)
        elif fact.subagent_id is not None:
            prior = peers.get(fact.subagent_id)
            # ``peer_dispatch_failed`` and a failed summary describe one terminal; failure wins.
            peer_status = "failed" if (prior is not None and prior.status == "failed") else fact.peer_status
            peers[fact.subagent_id] = _Peer(
                subagent_id=fact.subagent_id,
                profile=fact.profile or (prior.profile if prior else None),
                status=peer_status,
                seq=max(fact.seq, prior.seq) if prior else fact.seq,
            )
            if fact.call_id is not None:
                peer_by_call[fact.call_id] = fact.subagent_id

    def owning_peer(fact: StepFact) -> _Peer | None:
        if fact.owner is not None and fact.owner in peers:
            return peers[fact.owner]
        if fact.owner is None and fact.call_id is not None and fact.call_id in peer_by_call:
            return peers.get(peer_by_call[fact.call_id])
        return None

    def later_success_same_tool(fact: StepFact) -> bool:
        return any(ok.seq > fact.seq and ok.owner == fact.owner and ok.tool_name == fact.tool_name and ok.step_id != fact.step_id for ok in successes)

    def peer_redispatched(peer: _Peer) -> bool:
        return peer.profile is not None and any(
            other.subagent_id != peer.subagent_id and other.profile == peer.profile and other.seq > peer.seq and other.status in _PEER_SUCCESS
            for other in peers.values()
        )

    issues = [fact for fact in last_done.values() if fact.state in _ISSUE_STATES]
    unresolved: list[StepFact] = []
    recovered = 0
    for fact in issues:
        peer = owning_peer(fact)
        spawn_step = peer is not None and fact.owner is None
        if peer is not None and peer.status == "failed" and peer_redispatched(peer) or not spawn_step and later_success_same_tool(fact):
            recovered += 1
        else:
            unresolved.append(fact)
    pending = [fact for step_id, fact in started.items() if step_id not in last_done]
    unresolved.extend(pending)

    affected = False
    if status == "completed":
        for fact in unresolved:
            peer = owning_peer(fact)
            if peer is not None and peer.status in ("failed", "succeeded_with_warnings"):
                affected = True
                break
            if fact.state == "partial":
                affected = True
                break
    return {
        "schema_version": STEP_OUTCOME_SCHEMA_VERSION,
        "issue_steps": len(issues),
        "recovered_steps": recovered,
        "unresolved_steps": len(issues) - recovered + len(pending),
        "answer_affected": affected,
    }


def step_outcome_field(bus: Any, status: object) -> dict[str, Any]:
    """``{"step_outcome": ...}`` for a terminal ``done``, or ``{}`` when unknown.

    Omitted on Terminal v1 (the adapter does not record facts), when the record cap was exceeded,
    on continuation generations (the bus did not see earlier generations' steps), when no facts
    were collected by this bus, and when computation fails.
    """

    try:
        if getattr(bus, "terminal_protocol_version", None) == 1:
            return {}
        facts = getattr(bus, "_step_outcome_facts", None)
        if not isinstance(facts, list) or getattr(bus, "_step_outcome_overflow", False):
            return {}
        if getattr(bus, "_step_outcome_partial", False):
            # Continuation generation: earlier generations' steps are not in this bus.
            return {}
        return {"step_outcome": summarize_step_outcome(facts, status=str(status) if status is not None else None)}
    except Exception:
        return {}


__all__ = [
    "STEP_OUTCOME_EVENT_TYPES",
    "STEP_OUTCOME_RECORD_CAP",
    "STEP_OUTCOME_SCHEMA_VERSION",
    "StepFact",
    "step_fact_from_event",
    "step_outcome_field",
    "summarize_step_outcome",
]

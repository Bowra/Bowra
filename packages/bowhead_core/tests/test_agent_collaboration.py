"""Real migrated storage tests for directed questions and completion barriers."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_collaboration import CollaborationPolicy, CollaborationStore, CollaborationUnavailable, EvidenceReference, OperationIdentity
from bowhead_core.agent_execution_binding import AgentExecutionBindingError, AgentExecutionBindingStore, ExactAgentExecutionBinding
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentProfile, _system_profile_snapshot
from bowhead_core.turn_log_store import TurnLogStore
from cryptography.fernet import Fernet
from test_agent_execution_binding import _setup

pytestmark = pytest.mark.integration


@dataclass
class CollaborationFixture:
    path: Path
    store: CollaborationStore
    bindings: AgentExecutionBindingStore
    worker: DocumentTurnOwner
    lead: str
    requester: str
    recipient: str
    clock: list[float]
    readable: set[str]
    executions: dict[str, ExactAgentExecutionBinding]

    async def authorize(self, _db, _worker, _sender, _recipient, content):
        # Controlled result-access adapter; production resource readers are
        # exercised separately from the store and runner mechanisms.
        if any(ref["identity"] not in self.readable for ref in content["references"]):
            raise CollaborationUnavailable("collaboration_material_unavailable")

    def identity(self, execution_id=None, step="one", call="call", frame="base"):
        return OperationIdentity(execution_id=execution_id or self.requester, frame_id=frame, model_step_id=step, native_call_id=call)

    async def ask(self, **kwargs):
        defaults = {
            "identity": self.identity(),
            "recipient_id": self.recipient,
            "body": "Which refund definition applies?",
            "authorize": self.authorize,
        }
        return await self.store.ask(self.worker, **(defaults | kwargs))

    async def reply(self, question, **kwargs):
        defaults = {
            "identity": self.identity(self.recipient),
            "question_id": question["operation_id"],
            "body": "Refunds exclude shipping.",
            "authorize": self.authorize,
        }
        return await self.store.reply(self.worker, **(defaults | kwargs))

    async def read(self, receipt):
        return await self.store.read(self.worker, operation_id=receipt["operation_id"], authorize=self.authorize)

    async def count(self):
        async with open_shared_ctx(self.path) as db, db.execute("SELECT COUNT(*) FROM agent_collaboration_operation") as cursor:
            return (await cursor.fetchone())[0]


@pytest.fixture
async def collab(tmp_path, request):
    fixture = await _setup(tmp_path)
    worker = DocumentTurnOwner(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        workspace_id=fixture.workspace_id,
        session_id="s_m3b",
        turn_id=fixture.turn_id,
        generation=1,
        owner_id="collaboration-worker",
    )
    sessions = SessionStore(fixture.db_path)
    assert await sessions.claim_chat_spawn_dispatch_for_worker(turn_id=worker.turn_id, owner_id=worker.owner_id, lease_seconds=600) == "claimed"
    bindings = AgentExecutionBindingStore(fixture.db_path)
    ids = []
    executions = {}
    for index, profile in enumerate(("lead", "compute", "compute")):
        binding = await bindings.admit_system_execution(
            team_id=worker.team_id,
            user_id=worker.user_id,
            turn_id=worker.turn_id,
            profile_name=profile,
            dispatch_key=f"call:collaboration-{index}",
            definition=_system_profile_snapshot(
                SubagentProfile(
                    name=profile,
                    llm_agent_type="analyst",
                    tool_whitelist=("probe", "subagent_spawn", "subagent_return", "clarification_ask"),
                    system_prompt="Complete your assigned task using verified evidence.",
                    max_iterations=8,
                )
            ),
            manifest_fingerprint="sha256:" + "1" * 64,
            override_version=None,
            system_required=True,
            enforce_current_override=False,
        )
        await bindings.mark_running(binding.execution_id)
        ids.append(binding.execution_id)
        executions[binding.execution_id] = binding
    clock = [time.time()]
    store = CollaborationStore(fixture.db_path, cipher=ReferenceCipher(Fernet.generate_key().decode()), clock=lambda: clock[0])
    await store.admit(worker, owner_execution_id=ids[0], policy=getattr(request, "param", CollaborationPolicy()))
    for execution_id in ids:
        await store.join(
            worker,
            execution_id=execution_id,
            assignment="Verify assigned evidence.",
            expected_output="Cited evidence.",
            assistance_boundary="Bounded questions.",
        )
    return CollaborationFixture(fixture.db_path, store, bindings, worker, *ids, clock, {"refund-policy"}, executions)


async def test_reply_does_not_resolve_requester_or_root_completion(collab):
    question = await collab.ask()
    assert (await collab.read(question))["dependency_status"] == "open"
    for execution_id in (collab.recipient, collab.requester, collab.lead):
        with pytest.raises(AgentExecutionBindingError, match="agent_collaboration_unresolved"):
            await collab.bindings.mark_terminal(execution_id, status="completed")
    reply = await collab.reply(question)
    state = await collab.read(question)
    assert (state["response_status"], state["dependency_status"]) == ("answered", "open")
    await collab.bindings.mark_terminal(collab.recipient, status="completed")
    with pytest.raises(AgentExecutionBindingError, match="agent_collaboration_unresolved"):
        await collab.bindings.mark_terminal(collab.requester, status="completed")
    await collab.store.resolve(
        collab.worker,
        identity=collab.identity(step="verify"),
        question_id=question["operation_id"],
        reply_id=reply["operation_id"],
        disposition="resolved",
        reason="The cited policy matches this reporting period.",
        authorize=collab.authorize,
    )
    await collab.bindings.mark_terminal(collab.requester, status="completed")
    await collab.bindings.mark_terminal(collab.lead, status="completed")
    assert (await collab.read(question))["dependency_status"] == "resolved"


async def test_identical_retry_recovers_receipt_after_receiver_finishes(collab):
    question = await collab.ask()
    await collab.reply(question)
    await collab.bindings.mark_terminal(collab.recipient, status="completed")
    assert await collab.ask() == question
    assert await collab.count() == 2
    with pytest.raises(CollaborationUnavailable, match="operation_conflict"):
        await collab.ask(body="Changed arguments under the same invocation.")


@pytest.mark.parametrize("difference", [{"step": "two"}, {"frame": "coordination"}])
async def test_provider_reusing_a_call_id_in_another_step_or_frame_is_distinct(collab, difference):
    first = await collab.ask(required=False)
    second = await collab.ask(identity=collab.identity(**difference), required=False)
    assert first["operation_id"] != second["operation_id"]
    assert await collab.count() == 2


async def test_concurrent_retry_creates_one_question_and_one_publication(collab):
    first, second = await asyncio.gather(collab.ask(), collab.ask())
    assert first == second
    assert await collab.count() == 1
    assert len(await collab.store.pending_publications(collab.worker)) == 1


async def test_question_acceptance_and_recipient_completion_have_one_winner(collab):
    outcomes = await asyncio.gather(collab.ask(), collab.bindings.mark_terminal(collab.recipient, status="completed"), return_exceptions=True)
    accepted, terminal = outcomes
    if isinstance(accepted, dict):
        assert isinstance(terminal, AgentExecutionBindingError)
        assert terminal.code == "agent_collaboration_unresolved"
        assert await collab.count() == 1
    else:
        assert isinstance(accepted, CollaborationUnavailable)
        assert terminal is None
        assert await collab.count() == 0


async def test_failed_authorization_rolls_back_and_revocation_blocks_read_and_retry(collab):
    reference = EvidenceReference(identity="refund-policy", version="v1")
    question = await collab.ask(references=(reference,))
    collab.readable.clear()
    for work in (collab.read(question), collab.ask(references=(reference,))):
        with pytest.raises(CollaborationUnavailable, match="material_unavailable"):
            await work
    assert await collab.count() == 1
    with pytest.raises(CollaborationUnavailable, match="material_unavailable"):
        await collab.ask(identity=collab.identity(step="new"), references=(reference,))
    assert await collab.count() == 1


async def test_payload_bound_is_utf8_bytes_and_has_no_partial_receipt(collab):
    with pytest.raises(CollaborationUnavailable, match="payload_limit"):
        await collab.ask(body="€" * 3000)
    assert await collab.count() == 0
    with pytest.raises(CollaborationUnavailable, match="payload_limit"):
        await collab.ask(references=tuple(EvidenceReference(identity="refund-policy", version=str(i)) for i in range(9)))
    assert await collab.count() == 0


async def test_only_original_recipient_can_answer_and_only_owner_can_waive(collab):
    question = await collab.ask()
    with pytest.raises(CollaborationUnavailable, match="question_unavailable"):
        await collab.reply(question, identity=collab.identity(collab.lead))
    with pytest.raises(CollaborationUnavailable, match="resolution_forbidden"):
        await collab.store.resolve(
            collab.worker,
            identity=collab.identity(step="waive"),
            question_id=question["operation_id"],
            reply_id=None,
            disposition="waived",
            reason="Another specialist should own this instead.",
            authorize=collab.authorize,
        )
    await collab.store.resolve(
        collab.worker,
        identity=collab.identity(collab.lead, step="waive"),
        question_id=question["operation_id"],
        reply_id=None,
        disposition="waived",
        reason="Deliver with the missing-policy limitation.",
        authorize=collab.authorize,
    )
    assert (await collab.read(question))["dependency_status"] == "waived"
    with pytest.raises(CollaborationUnavailable, match="assignment_conflict"):
        await collab.store.join(
            collab.worker, execution_id=collab.requester, assignment="Transfer work.", expected_output="Different.", assistance_boundary="Unlimited."
        )


async def test_deadline_closes_the_dependency_as_a_limitation_never_as_satisfied(collab):
    """116 t_649dfe4abb48: an expired required question kept the requester's dependency open,
    nobody was prompted to close it, and the requester could never complete."""
    question = await collab.ask()
    collab.clock[0] += 61
    expired = await collab.store.expire(collab.worker)
    assert len(expired) == 1 and await collab.store.expire(collab.worker) == []
    state = await collab.read(question)
    assert (state["response_status"], state["dependency_status"]) == ("unavailable", "blocked")
    assert "limitation" in (await collab.read(expired[0]))["body"]
    with pytest.raises(CollaborationUnavailable, match="reply_closed"):
        await collab.reply(question)
    with pytest.raises(CollaborationUnavailable, match="dependency_closed"):
        await collab.store.resolve(
            collab.worker,
            identity=collab.identity(collab.lead, step="late-waive"),
            question_id=question["operation_id"],
            reply_id=None,
            disposition="waived",
            reason="Too late.",
            authorize=collab.authorize,
        )
    assert await collab.store.obligations(collab.worker, execution_id=collab.requester) == []
    await collab.bindings.mark_terminal(collab.recipient, status="completed")
    await collab.bindings.mark_terminal(collab.requester, status="completed")
    await collab.bindings.mark_terminal(collab.lead, status="completed")


async def test_unanswered_owner_question_closes_only_for_the_owner_and_only_once(collab):
    to_owner = await collab.ask(recipient_id=collab.lead, body="Can you grant the renderer authority?")
    to_peer = await collab.ask(identity=collab.identity(step="peer"), body="Which ledger period applies?")
    receipts = await collab.store.close_unanswered_owner_questions(collab.worker, question_ids=(to_owner["operation_id"], to_peer["operation_id"]))
    assert [(item["action"], item.get("outcome"), item.get("disposition")) for item in receipts] == [
        ("reply", "unavailable", None),
        ("resolve", None, "blocked"),
    ]
    assert all(item["sender_id"] == collab.lead and item["recipient_id"] == collab.requester for item in receipts)
    assert "limitation" in (await collab.read(receipts[0]))["body"]
    owner_state, peer_state = await collab.read(to_owner), await collab.read(to_peer)
    assert (owner_state["response_status"], owner_state["dependency_status"]) == ("unavailable", "blocked")
    assert (peer_state["response_status"], peer_state["dependency_status"]) == ("pending", "open")
    assert await collab.store.close_unanswered_owner_questions(collab.worker, question_ids=(to_owner["operation_id"],)) == []
    assert [item["question_id"] for item in await collab.store.obligations(collab.worker, execution_id=collab.requester)] == [to_peer["operation_id"]]


async def test_receipt_does_not_expose_plaintext_and_survives_store_recreation(collab):
    question = await collab.ask()
    async with (
        open_shared_ctx(collab.path) as db,
        db.execute("SELECT content_ciphertext,receipt_json FROM agent_collaboration_operation") as cursor,
    ):
        ciphertext, receipt = await cursor.fetchone()
    assert "refund definition" not in ciphertext + receipt
    restarted = CollaborationStore(collab.path, cipher=collab.store.cipher, clock=lambda: collab.clock[0])
    assert (await restarted.read(collab.worker, operation_id=question["operation_id"], authorize=collab.authorize))[
        "body"
    ] == "Which refund definition applies?"
    assert len(await restarted.pending_publications(collab.worker)) == 1


async def test_publication_ack_requires_the_actual_matching_persisted_event(collab):
    question = await collab.ask()
    with pytest.raises(CollaborationUnavailable, match="publication_unproven"):
        await collab.store.acknowledge_publication(collab.worker, operation_id=question["operation_id"])
    intent = (await collab.store.pending_publications(collab.worker))[0]
    row = await TurnLogStore(collab.path).record(turn_id=collab.worker.turn_id, **intent)
    assert row is not None
    await collab.store.acknowledge_publication(collab.worker, operation_id=question["operation_id"])
    await collab.store.acknowledge_publication(collab.worker, operation_id=question["operation_id"])
    assert await collab.store.pending_publications(collab.worker) == []


async def test_stale_worker_and_stop_fence_new_questions(collab):
    stale = collab.worker.model_copy(update={"generation": 2})
    with pytest.raises(CollaborationUnavailable, match="worker_unavailable"):
        await collab.store.ask(stale, identity=collab.identity(), recipient_id=collab.recipient, body="Question", authorize=collab.authorize)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id=?", ("2026-10-09", collab.worker.turn_id))
        await db.commit()
    with pytest.raises(CollaborationUnavailable, match="worker_unavailable"):
        await collab.ask()
    assert await collab.count() == 0


async def test_history_and_publication_reads_cannot_cross_turn_session_scope(collab):
    question = await collab.ask()
    sessions = SessionStore(collab.path)
    await sessions.ensure_session(session_id="s_other", workspace_id=collab.worker.workspace_id, user_id=collab.worker.user_id)
    wrong = collab.worker.model_copy(update={"session_id": "s_other"})
    with pytest.raises(CollaborationUnavailable):
        await collab.store.pending_publications(wrong)
    with pytest.raises(CollaborationUnavailable):
        await collab.store.read(wrong, operation_id=question["operation_id"], authorize=collab.authorize)


async def test_parent_completion_cannot_bypass_an_open_dependency(collab):
    await collab.ask()
    async with open_shared_ctx(collab.path) as db:
        with pytest.raises(aiosqlite.IntegrityError, match="unresolved collaboration obligations"):
            await db.execute("UPDATE turns SET status='completed' WHERE id=?", (collab.worker.turn_id,))
        await db.rollback()


@pytest.mark.parametrize("collab", [CollaborationPolicy(max_operations=3)], indirect=True)
async def test_new_work_cannot_spend_the_reserved_reply_and_resolution_slots(collab):
    question = await collab.ask()
    with pytest.raises(CollaborationUnavailable, match="operation_limit"):
        await collab.ask(identity=collab.identity(step="extra"), required=False)
    reply = await collab.reply(question)
    await collab.store.resolve(
        collab.worker,
        identity=collab.identity(step="verify"),
        question_id=question["operation_id"],
        reply_id=reply["operation_id"],
        disposition="resolved",
        reason="Verified against the original requirement.",
        authorize=collab.authorize,
    )
    assert await collab.count() == 3


async def test_admission_retry_does_not_reset_deadline_or_change_policy(collab):
    collab.clock[0] += 899
    await collab.store.admit(collab.worker, owner_execution_id=collab.lead, policy=CollaborationPolicy())
    with pytest.raises(CollaborationUnavailable, match="admission_conflict"):
        await collab.store.admit(collab.worker, owner_execution_id=collab.lead, policy=CollaborationPolicy(max_questions=2))
    collab.clock[0] += 2
    with pytest.raises(CollaborationUnavailable, match="task_unavailable"):
        await collab.ask()


async def test_followups_keep_the_original_family_deadline_and_limit(collab):
    original = await collab.ask(required=False)
    first = await collab.ask(identity=collab.identity(step="follow-one"), required=False, follow_up_to=original["operation_id"])
    await collab.ask(identity=collab.identity(step="follow-two"), required=False, follow_up_to=first["operation_id"])
    with pytest.raises(CollaborationUnavailable, match="followup_limit"):
        await collab.ask(identity=collab.identity(step="follow-three"), required=False, follow_up_to=original["operation_id"])
    collab.clock[0] += 61
    assert len(await collab.store.expire(collab.worker)) == 3


async def test_resolution_rechecks_reply_material_and_requires_its_exact_identity(collab):
    question = await collab.ask()
    reply = await collab.reply(question, references=(EvidenceReference(identity="refund-policy", version="v1"),))
    collab.readable.clear()
    with pytest.raises(CollaborationUnavailable, match="material_unavailable"):
        await collab.store.resolve(
            collab.worker,
            identity=collab.identity(step="verify"),
            question_id=question["operation_id"],
            reply_id=reply["operation_id"],
            disposition="resolved",
            reason="Checked.",
            authorize=collab.authorize,
        )
    assert (await collab.read(question))["dependency_status"] == "open"
    assert await collab.count() == 2


async def test_refusal_is_not_a_successful_dependency_resolution(collab):
    question = await collab.ask()
    reply = await collab.reply(question, unavailable=True, body="I cannot establish that definition.")
    with pytest.raises(CollaborationUnavailable, match="evidence_unavailable"):
        await collab.store.resolve(
            collab.worker,
            identity=collab.identity(step="verify"),
            question_id=question["operation_id"],
            reply_id=reply["operation_id"],
            disposition="resolved",
            reason="The peer responded.",
            authorize=collab.authorize,
        )
    assert (await collab.read(question))["dependency_status"] == "open"


@pytest.mark.parametrize("collab", [CollaborationPolicy(max_operations=4)], indirect=True)
async def test_server_expiry_cannot_spend_a_required_resolution_reservation(collab):
    required = await collab.ask()
    await collab.ask(identity=collab.identity(step="optional"), required=False)
    collab.clock[0] += 61
    assert len(await collab.store.expire(collab.worker)) == 2
    # Expiry itself records the limitation without spending the operation allowance;
    # a later owner disposition finds it closed rather than over the limit.
    assert (await collab.read(required))["dependency_status"] == "blocked"
    with pytest.raises(CollaborationUnavailable, match="dependency_closed"):
        await collab.store.resolve(
            collab.worker,
            identity=collab.identity(collab.lead, step="close"),
            question_id=required["operation_id"],
            reply_id=None,
            disposition="blocked",
            reason="No verified answer was available before the deadline.",
            authorize=collab.authorize,
        )


async def test_owner_can_close_a_revoked_dependency_without_reading_its_body(collab):
    question = await collab.ask(references=(EvidenceReference(identity="refund-policy", version="v1"),))
    collab.readable.clear()
    receipt = await collab.store.resolve(
        collab.worker,
        identity=collab.identity(collab.lead, step="limit"),
        question_id=question["operation_id"],
        reply_id=None,
        disposition="blocked",
        reason="Required material is no longer available.",
        authorize=collab.authorize,
    )
    assert receipt["disposition"] == "blocked"
    assert (await collab.read(receipt))["body"] == "Required material is no longer available."
    with pytest.raises(CollaborationUnavailable, match="material_unavailable"):
        await collab.read(question)


async def test_answer_identity_cannot_be_rewritten_or_reopened(collab):
    question = await collab.ask()
    await collab.reply(question)
    async with open_shared_ctx(collab.path) as db:
        for assignment in ("reply_id='aco_replacement'", "response_status='pending'"):
            with pytest.raises(aiosqlite.IntegrityError, match="operation identity is immutable"):
                await db.execute(f"UPDATE agent_collaboration_operation SET {assignment} WHERE id=?", (question["operation_id"],))
            await db.rollback()

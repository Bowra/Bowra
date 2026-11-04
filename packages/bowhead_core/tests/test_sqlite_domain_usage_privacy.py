"""Late usage writers must respect the committed account retirement barrier."""

from __future__ import annotations

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.account_deletion import request_account_deletion
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.physical_usage import PhysicalUsageObserver
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.subagent_draft_accounting import SubagentDraftCreditAccountant
from bowhead_core.user_store import UserStore
from bowhead_router.usage import usage_context
from cryptography.fernet import Fernet
from test_document_usage import SPEC
from test_sqlite_domain_stores import domain_directory as domain_directory


@pytest.mark.asyncio
async def test_retirement_blocks_queued_and_interrupted_usage_publications(domain_directory):
    core = domain_directory / "bowhead.db"
    user_id = "u_late_usage"
    async with open_shared_ctx(core) as db:
        await db.execute("INSERT INTO users(id,team_id,email,credit_balance,created_at) VALUES (?,'team_1','late@example.test',100,'t')", (user_id,))
        await db.commit()
    async with open_shared_ctx(domain_database_path(core, "execution")) as db:
        await db.execute(
            "INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents) "
            "VALUES ('s_late_usage','ws_1',?,'Session','t','t','active',0,0,'[]')",
            (user_id,),
        )
        await db.commit()
    await SessionStore(core).record_turn(turn_id="t_late_usage", session_id="s_late_usage", user_text="Question")
    accountant = SubagentDraftCreditAccountant(core)
    identity = {"team_id": "team_1", "owner_user_id": user_id, "request_key_hash": "late-usage-draft"}
    usage = {**identity, "draft_id": None, "input_tokens": 700, "output_tokens": 0, "attempts": 1}
    await accountant.preflight(**identity)
    # The core debit committed, but the process stopped before execution publication.
    await accountant._settle_core(**usage)
    users = UserStore(core)
    assert await users.record_llm_usage_events([{"user_id": user_id, "reason": "retained-before-retirement"}]) == 1
    await request_account_deletion(users, user_id)
    await accountant.settle(**usage)
    assert (
        await users.record_llm_usage_events(
            [
                {"user_id": user_id, "reason": "late-retired"},
                {"user_id": "u_1", "reason": "late-active"},
                {"reason": "late-anonymous"},
            ]
        )
        == 2
    )
    assert await users.rollup_llm_usage_daily() == 2
    observer = PhysicalUsageObserver(str(core), cipher=ReferenceCipher(Fernet.generate_key().decode()), turn_id="t_late_usage", generation=1)
    with (
        usage_context(team_id="team_1", user_id=user_id, workspace_id="ws_1", turn_id="t_late_usage", reason="turn"),
        pytest.raises(ValueError, match="physical_usage_scope_unavailable"),
    ):
        await observer.begin(SPEC, request=None, body={}, family="openai")
    async with (
        open_shared_ctx(domain_database_path(core, "execution")) as db,
        db.execute("SELECT reason FROM llm_usage_events WHERE user_id=?", (user_id,)) as cursor,
    ):
        assert await cursor.fetchall() == [("retained-before-retirement",)]
    async with open_shared_ctx(core) as db, db.execute("SELECT count(*) FROM llm_usage_daily WHERE user_id=?", (user_id,)) as cursor:
        assert await cursor.fetchone() == (0,)

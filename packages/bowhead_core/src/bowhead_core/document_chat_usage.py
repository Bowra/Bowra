"""Inclusive document usage for display; never a credit-deduction input."""

from bowhead_core._db import open_shared_ctx


async def document_usage_projection(
    db_path, *, turn_id: str, generation_input: int | None = None, generation_output: int | None = None
) -> dict | None:
    async with open_shared_ctx(db_path) as db:
        await db.execute("BEGIN")
        return await document_usage_projection_in_tx(db, turn_id=turn_id, generation_input=generation_input, generation_output=generation_output)


async def document_usage_projection_in_tx(
    db, *, turn_id: str, generation_input: int | None = None, generation_output: int | None = None
) -> dict | None:
    """Read through a caller's snapshot, including crash-repair transactions."""
    async with db.execute("SELECT 1 FROM sqlite_master WHERE name='document_turn_dispatches'") as cursor:
        if await cursor.fetchone() is None:
            return None
    async with db.execute(
        """SELECT t.tokens_input,t.tokens_output,t.generation_base_tokens_input,t.generation_base_tokens_output
        FROM turns t JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id
        WHERE t.id=? AND EXISTS(SELECT 1 FROM document_turn_dispatches d JOIN document_jobs j ON j.id=d.job_id
          WHERE j.origin_turn_id=t.id AND j.session_id=s.id AND j.requested_by=s.user_id
          AND j.workspace_id=s.workspace_id AND j.team_id=w.team_id)""",
        (turn_id,),
    ) as cursor:
        turn = await cursor.fetchone()
    if turn is None:
        return None
    async with db.execute(
        """SELECT COALESCE(SUM(r.tokens_input),0),COALESCE(SUM(r.tokens_output),0),
        COALESCE(SUM(CASE WHEN r.job_id IS NULL AND u.receipt_state<>'not_applied' THEN 1 ELSE 0 END),0)
        FROM document_turn_dispatches d JOIN document_jobs j ON j.id=d.job_id
        JOIN document_physical_receipt_links l ON l.job_id=j.id
        JOIN llm_usage_events u ON u.id=l.receipt_id AND u.turn_id=j.origin_turn_id
          AND u.user_id=j.requested_by AND u.team_id=j.team_id AND u.workspace_id=j.workspace_id
        LEFT JOIN document_usage_receipts r ON r.job_id=l.job_id AND r.attempt_no=l.attempt_no AND r.call_no=l.call_no
        WHERE j.origin_turn_id=?""",
        (turn_id,),
    ) as cursor:
        document = await cursor.fetchone()
    root_input = turn[0] if generation_input is None else turn[2] + generation_input
    root_output = turn[1] if generation_output is None else turn[3] + generation_output
    return {
        "tokens_input": int(root_input or 0) + document[0],
        "tokens_output": int(root_output or 0) + document[1],
        "partial": document[2] > 0,
    }

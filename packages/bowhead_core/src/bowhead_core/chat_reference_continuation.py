"""Select the continuation owned by the admitted reference contract."""

from __future__ import annotations

import json

import aiosqlite

from bowhead_core.chat_reference_storage import _row


async def is_lead(db: aiosqlite.Connection, turn_id: str) -> bool:
    row = await _row(db, "SELECT plan_json FROM chat_reference_plans WHERE turn_id=?", (turn_id,))
    if row is None:
        return False
    try:
        return json.loads(row["plan_json"]).get("orchestration_contract") == "lead-delegation.v1"
    except (AttributeError, TypeError, ValueError):
        return False


async def pause_ready(db: aiosqlite.Connection, *, turn_id: str, clarification_id: str | None = None) -> bool:
    if await is_lead(db, turn_id):
        from bowhead_core.chat_reference_plan import proposal_pause_ready

        if clarification_id is None and await proposal_pause_ready(db, turn_id=turn_id):
            return True
        from bowhead_core.chat_reference_lead_continuation import pause_ready as lead_ready
        from bowhead_core.chat_reference_lead_postdispatch import pause_ready as post_ready

        return await post_ready(db, turn_id=turn_id, clarification_id=clarification_id) or await lead_ready(
            db, turn_id=turn_id, clarification_id=clarification_id
        )
    from bowhead_core.chat_reference_bi_continuation import pause_ready as bi_ready

    return await bi_ready(db, turn_id=turn_id, clarification_id=clarification_id)


async def pause_recoverable(db: aiosqlite.Connection, *, turn_id: str, reference_store=None) -> bool:
    if await is_lead(db, turn_id):
        return await pause_ready(db, turn_id=turn_id)
    if reference_store is not None:
        return await reference_store.bi_pause_recoverable_in_connection(db, turn_id=turn_id)
    return await pause_ready(db, turn_id=turn_id)


async def reserve_resume(db: aiosqlite.Connection, *, turn_id: str, clarification_id: str, generation: int) -> None:
    if await is_lead(db, turn_id):
        if not await pause_ready(db, turn_id=turn_id, clarification_id=clarification_id):
            raise ValueError("reference_continuation_unavailable")
        return
    from bowhead_core.chat_reference_bi_continuation import reserve_resume as bi_reserve

    await bi_reserve(db, turn_id=turn_id, clarification_id=clarification_id, generation=generation)


async def require_resume(db: aiosqlite.Connection, *, turn_id: str, generation: int):
    if await is_lead(db, turn_id):
        from bowhead_core.chat_reference_lead_continuation import require_resume as lead_require
        from bowhead_core.chat_reference_lead_postdispatch import read_pause as post_read
        from bowhead_core.chat_reference_lead_postdispatch import require_resume as post_require

        post = await post_read(db, turn_id)
        if post is not None and generation > post["delegation_generation"]:
            return await post_require(db, turn_id=turn_id, generation=generation)
        return await lead_require(db, turn_id=turn_id, generation=generation)
    from bowhead_core.chat_reference_bi_continuation import require_resume as bi_require

    return await bi_require(db, turn_id=turn_id, generation=generation)


async def release_resume(db: aiosqlite.Connection, *, turn_id: str, generation: int) -> None:
    if await is_lead(db, turn_id):
        from bowhead_core.chat_reference_lead_continuation import release_resume as lead_release
        from bowhead_core.chat_reference_lead_postdispatch import read_pause as post_read
        from bowhead_core.chat_reference_lead_postdispatch import release_resume as post_release

        post = await post_read(db, turn_id)
        if post is not None and generation > post["delegation_generation"]:
            await post_release(db, turn_id=turn_id, generation=generation)
            return
        await lead_release(db, turn_id=turn_id, generation=generation)
        return
    from bowhead_core.chat_reference_bi_continuation import release_resume as bi_release

    await bi_release(db, turn_id=turn_id, generation=generation)

"""Document-owned bounded pipeline, independent of the completed chat Turn."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable

import anyio
from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.invocation import physical_invocation_scope
from bowhead_router.types import LLMResponse
from bowhead_router.usage import usage_context

from bowhead_core._db import open_shared_ctx
from bowhead_core.documents.delivery import DocumentDeliveryStore, _fence
from bowhead_core.documents.executor import DocumentExecutor, WorkerInput
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.pptx_plan import SYSTEM_PROMPT
from bowhead_core.documents.pptx_planning import REPAIRABLE_ERRORS
from bowhead_core.documents.request import DocumentRequestError, DocumentScope
from bowhead_core.documents.store import _json
from bowhead_core.documents.usage import DocumentModelHook
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx


async def run_document(
    *,
    delivery: DocumentDeliveryStore,
    executor: DocumentExecutor,
    scope: DocumentScope,
    job_id: str,
    invoke: Callable[[str, str], Awaitable[LLMResponse]],
    expected_image_id: str,
) -> None:
    """The caller supplies a currently authorized team model, never a fallback.

    The physical hook rejects transport replay. One completed malformed plan
    may be repaired; unknown outcomes and render failures are never retried.
    This routine never publishes or issues a user confirmation.
    """
    owner = await delivery.claim(scope=scope, job_id=job_id)
    if owner is None:
        return
    try:
        inputs_store = DocumentInputStore(delivery.artifacts)
        frozen = await inputs_store.freeze(scope=scope, job_id=job_id)
        async with open_shared_ctx(delivery.path) as db:
            await db.execute("BEGIN")
            await _fence(db, scope, job_id, owner)
            async with db.execute("SELECT origin_turn_id FROM document_jobs WHERE id=?", (job_id,)) as cursor:
                turn_id = (await cursor.fetchone())[0]
            source = await _read_chat_turn_user_source_in_tx(
                db, turn_id=turn_id, expected_user_id=scope.user_id, expected_workspace_id=scope.workspace_id
            )
            async with db.execute(
                "SELECT instruction FROM document_continuations WHERE job_id=? UNION ALL SELECT instruction FROM document_revisions WHERE job_id=?",
                (job_id, job_id),
            ) as cursor:
                continuation = await cursor.fetchone()
            async with db.execute("SELECT artifact_id,filename FROM document_input_copies WHERE job_id=?", (job_id,)) as cursor:
                names = dict(await cursor.fetchall())
        if source is None:
            raise DocumentRequestError("document_source_unavailable")
        instruction = continuation[0] if continuation else source.user_text
        inputs = tuple(
            [
                WorkerInput(names[item.artifact_id], item.role, await inputs_store.read(scope=scope, job_id=job_id, artifact_id=item.artifact_id))
                for item in frozen
            ]
        )
        inventory = await executor.run(
            run_id=hashlib.sha256((job_id + ":inventory").encode()).hexdigest(),
            inputs=inputs,
            instruction=instruction,
            operation="inventory",
            expected_image_id=expected_image_id,
        )
        if inventory.record["kind"] != "context":
            raise OfficePackageError(inventory.record.get("error_code", "document_worker_failed"))
        context = inventory.record["context"]
        await delivery.transition(scope=scope, job_id=job_id, owner=owner, state="planning", image_id=inventory.image_id, context_json=_json(context))
        prompt = context["payload"]
        for call_no in (1, 2):
            hook = DocumentModelHook(str(delivery.path), scope, job_id, owner, call_no)
            with (
                usage_context(team_id=scope.team_id, user_id=scope.user_id, reason="template_document"),
                physical_invocation_scope(hook),
                anyio.fail_after(90),
            ):
                reply = await invoke(SYSTEM_PROMPT, prompt)
            # A provider adapter without the physical contract must not silently
            # let a nominal response bypass durable usage evidence.
            async with open_shared_ctx(delivery.path) as db:
                await db.execute("BEGIN")
                await _fence(db, scope, job_id, owner)
                async with db.execute(
                    "SELECT state FROM document_model_calls WHERE job_id=? AND attempt_no=1 AND call_no=?", (job_id, call_no)
                ) as cursor:
                    receipt = await cursor.fetchone()
                if receipt is None or receipt[0] != "completed":
                    raise DocumentRequestError("document_usage_unresolved")
            if reply.finish_reason != "stop" or len(reply.content.encode()) > 64_000:
                raise OfficePackageError("planning_incomplete_response")
            await delivery.transition(scope=scope, job_id=job_id, owner=owner, state="rendering", response=reply.content)
            output = await executor.run(
                run_id=hashlib.sha256((job_id + f":apply:{call_no}").encode()).hexdigest(),
                inputs=inputs,
                instruction=instruction,
                operation="apply_verify",
                response=reply.content,
                context_digest=context["digest"],
                expected_image_id=inventory.image_id,
            )
            if output.record["kind"] == "question":
                await delivery.transition(scope=scope, job_id=job_id, owner=owner, state="question", question=output.record["review"]["question"])
                return
            if output.record["kind"] == "draft":
                await delivery.save_draft(scope=scope, job_id=job_id, owner=owner, result=output)
                return
            error = output.record.get("error_code", "document_worker_failed")
            if call_no == 2 or error not in REPAIRABLE_ERRORS:
                raise OfficePackageError(error)
            await delivery.transition(scope=scope, job_id=job_id, owner=owner, state="planning", error_code=error)
            prompt = _json(
                {
                    "input": json.loads(context["payload"]),
                    "repair": {
                        "error": error,
                        "previous_response": reply.content,
                        "instruction": "Correct the protocol failure once. Recheck the original request; do not invent missing business facts.",
                    },
                }
            )
    except InvocationOutcomeUnknownError:
        async with (
            open_shared_ctx(delivery.path) as db,
            db.execute("SELECT 1 FROM document_usage_receipts WHERE job_id=? AND settled_at IS NULL", (job_id,)) as cursor,
        ):
            pending = await cursor.fetchone() is not None
        code = "document_usage_settlement_pending" if pending else "document_usage_unresolved"
        await delivery.fail(job_id=job_id, owner=owner, code=code)
    except (OfficePackageError, DocumentRequestError) as exc:
        await delivery.fail(job_id=job_id, owner=owner, code=exc.code)
    except Exception:
        # No provider body, filename or business content enters the public error.
        await delivery.fail(job_id=job_id, owner=owner, code="document_execution_failed")
    except BaseException:
        with anyio.CancelScope(shield=True):
            await delivery.fail(job_id=job_id, owner=owner, code="document_execution_interrupted")
        raise

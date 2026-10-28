"""Durable runner integration; native rendering is an explicit local smoke.

The model transport is the external test boundary. HostWorker runs the actual
fixed worker protocol on synthetic inputs, but is NOT isolation certification.
Production always uses DocumentExecutor and its fixed Docker worker.
"""

import hashlib
import json
import os

import anyio
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.executor import read_result
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.request import DocumentProposal, InputRoleProposal, UserQuote
from bowhead_core.documents.runner import run_document
from bowhead_core.documents.worker import WORKER_PROFILE, execute
from bowhead_router.invocation import begin_physical_invocation
from bowhead_router.types import LLMResponse, TokenUsage
from test_document_pptx_plan import case as case
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration
IMAGE = "sha256:" + "b" * 64


@pytest.mark.skipif(not os.environ.get("BOWHEAD_DOCUMENT_DOCKER_IMAGE"), reason="Requires an explicitly built local document worker")
async def test_isolated_docker_draft_publishes_exact_files(ctx, case, tmp_path, monkeypatch):
    import importlib.util
    import subprocess
    from pathlib import Path

    import httpx2
    from bowhead_core.documents.executor import DocumentExecutor

    module_path = Path(__file__).resolve().parents[3] / "deploy/sandbox-executor/document_runs.py"
    spec = importlib.util.spec_from_file_location("native_document_runs", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {
        "IMAGE": os.environ["BOWHEAD_DOCUMENT_DOCKER_IMAGE"],
        "MEMORY_MB": "2048",
        "TMPFS_MB": "256",
        "PIDS": "128",
        "MAX_TIMEOUT": "120",
        "RUNTIME": "runc",
    }.items():
        monkeypatch.setenv("BOWHEAD_SANDBOX_" + key, value)
    root = tmp_path / "docker-worker"
    root.mkdir()
    runs = module.DocumentRuns(root)
    image_id = runs.readiness()["image_id"]
    dispatched = []

    async def dispatch(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            directory = root / ("document-" + payload["run_id"])
            # macOS tests run as the host developer, unlike production Gateway
            # UID 1000. Normalize only this synthetic fixture's mounted roots.
            (directory / "input").chmod(0o755)
            (directory / "output").chmod(0o777)
            record = await anyio.to_thread.run_sync(runs.start, payload)
            inspected = json.loads(subprocess.check_output(["docker", "inspect", record["container"]]))[0]  # noqa: S607
            assert inspected["HostConfig"]["NetworkMode"] == "none"
            assert inspected["HostConfig"]["ReadonlyRootfs"] is True
            assert inspected["Config"]["User"] == "1000:1000"
            assert inspected["Image"] == image_id
            dispatched.append(record["run_id"])
        else:
            record = await anyio.to_thread.run_sync(runs.get, request.url.path.rsplit("/", 1)[-1])
        return httpx2.Response(200, json=record)

    original_client = httpx2.AsyncClient
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kwargs: original_client(transport=httpx2.MockTransport(dispatch), **kwargs))
    job_id = await setup_job(ctx, case)
    delivery = DocumentDeliveryStore(ctx.artifacts)
    calls = []
    await run_document(
        delivery=delivery,
        executor=DocumentExecutor(root=root, url="http://executor", token="fixture"),
        scope=SCOPE,
        job_id=job_id,
        invoke=transport([json.dumps(case[4])], calls),
        expected_image_id=image_id,
    )
    draft = await delivery.get(scope=SCOPE, job_id=job_id)
    assert draft["state"] == "review", draft
    assert draft["draft"]["preview"]["text_and_geometry"] == "passed"
    published = await delivery.confirm(scope=SCOPE, job_id=job_id, command_id="docker-confirm", draft_digest=draft["draft_digest"])
    assert published["state"] == "published"
    for name in ("candidate.pptx", "candidate.pdf"):
        data, _ = await delivery.read_file(scope=SCOPE, job_id=job_id, name=name, preview=False)
        assert hashlib.sha256(data).hexdigest() == draft["draft"]["files"][name]["sha256"]
    assert len(calls) == 1 and len(dispatched) == 2


class HostWorker:
    def __init__(self, root):
        self.root = root
        self.runs = []

    async def run(
        self,
        *,
        run_id,
        inputs,
        instruction,
        operation,
        response=None,
        context_digest=None,
        expected_image_id=None,
        planner_profile="pptx-grounded-plan-v4",
    ):
        assert expected_image_id == IMAGE
        self.runs.append(operation)
        directory = self.root / run_id
        source, target = directory / "input", directory / "output"
        source.mkdir(parents=True)
        target.mkdir()
        control = {
            "profile": WORKER_PROFILE,
            "operation": operation,
            "instruction": instruction,
            "inputs": [{"name": item.name, "role": item.role, "sha256": hashlib.sha256(item.content).hexdigest()} for item in inputs],
            "response": response,
            "context_digest": context_digest,
        }
        if planner_profile != "pptx-grounded-plan-v4":
            control["planner_profile"] = planner_profile
        raw = json.dumps(control).encode()
        (source / "control.json").write_bytes(raw)
        for index, item in enumerate(inputs):
            (source / f"input-{index}").write_bytes(item.content)
        try:
            result = await anyio.to_thread.run_sync(execute, source, target, hashlib.sha256(raw).hexdigest())
        except OfficePackageError as exc:
            result = {"profile": WORKER_PROFILE, "kind": "error", "error_code": exc.code}
        (target / "result.json").write_text(json.dumps(result))
        return read_result(target, image_id=IMAGE, operation=operation, context_digest=context_digest, planner_profile=planner_profile)


async def setup_job(ctx, case):
    turn_id = "t_pipeline"
    text = case[1] + " The first file is the template; the second file supplies the data. Return editable PPTX and its PDF."
    artifacts = [
        await ctx.artifacts.put_bytes(content=content, filename=name, mime="application/octet-stream", created_by=SCOPE.user_id)
        for name, content in (("template.pptx", case[0]), ("counts.json", case[2][0].content))
    ]
    await ctx.sessions.ensure_session(session_id="s_pipeline", workspace_id=SCOPE.workspace_id, user_id=SCOPE.user_id)
    await ctx.sessions.record_turn(
        turn_id=turn_id,
        session_id="s_pipeline",
        user_text=text,
        user_source=ChatTurnUserSource(
            turn_id=turn_id,
            session_id="s_pipeline",
            user_id=SCOPE.user_id,
            workspace_id=SCOPE.workspace_id,
            user_text=text,
            attachment_ids=tuple(item.id for item in artifacts),
            attachment_refs=tuple({"fileId": item.id} for item in artifacts),
            parts=None,
        ),
    )
    await ctx.sessions.record_turn_attachments(turn_id=turn_id, artifact_ids=[item.id for item in artifacts])
    await ctx.sessions.pin_session_attachments(session_id="s_pipeline", artifact_ids=[item.id for item in artifacts])
    quote = UserQuote(start=0, text=text)
    proposal = DocumentProposal(
        evidence=quote,
        inputs=tuple(
            InputRoleProposal(artifact_id=item.id, role=role, evidence=quote, current_message_position=index)
            for index, (item, role) in enumerate(zip(artifacts, ("template", "data"), strict=True), 1)
        ),
        requested_outputs=("source", "pdf"),
    )
    receipt = await ctx.store.submit_from_turn(scope=SCOPE, turn_id=turn_id, command_id="pipeline", proposal=proposal)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE users SET credit_balance=100 WHERE id=?", (SCOPE.user_id,))
        await db.execute("UPDATE teams SET team_credit_balance=100 WHERE id=?", (SCOPE.team_id,))
        await db.commit()
    return receipt.job_id


def transport(replies, calls, *, unknown=False):
    async def invoke(system, prompt):
        calls.append(prompt)
        attempt = await begin_physical_invocation(
            provider="fixture-provider",
            model="fixture-model",
            adapter_contract_key="fixture.v1",
            mode="invoke",
            request={"system": system, "prompt": prompt},
        )
        if unknown:
            await attempt.unknown(reason_code="transport_disconnected")
            raise OSError("external transport unavailable")
        content = replies[len(calls) - 1]
        usage = TokenUsage(600, 100)
        assert await attempt.complete(usage=usage, response_digest=hashlib.sha256(content.encode()).hexdigest())
        return LLMResponse(content=content, usage=usage, provider="fixture-provider", model="fixture-model")

    return invoke


@pytest.mark.parametrize("repair", [False, True])
async def test_missing_information_is_durable_and_only_protocol_errors_repair(ctx, case, tmp_path, repair):
    job_id = await setup_job(ctx, case)
    worker, calls = HostWorker(tmp_path / "worker"), []
    question = json.dumps({"action": "ask", "reason": "comparison", "slots": ["s0"], "question": "Which comparison period?"})
    await run_document(
        delivery=DocumentDeliveryStore(ctx.artifacts),
        executor=worker,
        scope=SCOPE,
        job_id=job_id,
        invoke=transport((["invalid JSON"] if repair else []) + [question], calls),
        expected_image_id=IMAGE,
    )
    saved = await DocumentDeliveryStore(ctx.artifacts).get(scope=SCOPE, job_id=job_id)
    assert saved["state"] == "question" and saved["question"] == "Which comparison period?"
    assert len(calls) == (2 if repair else 1) and "draft" not in saved


async def test_unknown_transport_never_replays_and_is_not_zero_usage(ctx, case, tmp_path):
    job_id = await setup_job(ctx, case)
    worker, calls = HostWorker(tmp_path / "worker"), []
    delivery = DocumentDeliveryStore(ctx.artifacts)
    for _ in range(2):
        await run_document(
            delivery=delivery, executor=worker, scope=SCOPE, job_id=job_id, invoke=transport([], calls, unknown=True), expected_image_id=IMAGE
        )
    saved = await delivery.get(scope=SCOPE, job_id=job_id)
    assert saved["state"] == "failed" and saved["usage"]["pending"] is True
    assert len(calls) == 1 and worker.runs == ["inventory"]


async def test_question_successor_worker_receives_original_request_and_answer(ctx, case, tmp_path):
    from test_document_capability import runtime
    from test_document_recovery import turn

    parent = await setup_job(ctx, case)
    worker, calls = HostWorker(tmp_path / "worker"), []
    delivery = DocumentDeliveryStore(ctx.artifacts)
    response = json.dumps({"action": "ask", "reason": "comparison", "slots": ["s0"], "question": "Which comparison period?"})
    await run_document(delivery=delivery, executor=worker, scope=SCOPE, job_id=parent, invoke=transport([response], calls), expected_image_id=IMAGE)
    assert (await delivery.get(scope=SCOPE, job_id=parent))["state"] == "question"
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE turns SET status='completed' WHERE id='t_pipeline'")
        await db.commit()
    await ctx.sessions.finalize_turn(session_id="s_pipeline", current_turn_id="t_pipeline", next_turn_id=None)
    answer = "Use the same quarter last year."
    turn_id = await turn(ctx, "pipeline_answer", text=answer, session_id="s_pipeline", attach=False)
    successor = await ctx.store.answer_from_turn(
        scope=SCOPE,
        turn_id=turn_id,
        command_id="answer",
        parent_job_id=parent,
        evidence=UserQuote(text=answer),
        runtime=await runtime(ctx, turn_id=turn_id),
    )
    successor_calls = []
    await run_document(
        delivery=delivery,
        executor=worker,
        scope=SCOPE,
        job_id=successor.job_id,
        invoke=transport([response], successor_calls),
        expected_image_id=IMAGE,
    )
    saved = await delivery.get(scope=SCOPE, job_id=successor.job_id)
    assert saved["state"] == "question", saved
    instruction = json.loads(successor_calls[0])["instruction"]
    assert json.loads(calls[0])["instruction"] in instruction
    assert "Which comparison period?" in instruction and answer in instruction
    assert worker.runs == ["inventory", "apply_verify", "inventory", "apply_verify"]


@pytest.mark.skipif(os.environ.get("BOWHEAD_DOCUMENT_NATIVE_SMOKE") != "1", reason="Requires explicit native LibreOffice/Poppler smoke environment")
async def test_native_draft_confirm_exact_source_pdf_without_regeneration(ctx, case, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import httpx2
    from bowhead_core.user_store import UserStore
    from bowhead_gateway.document_service import DocumentService
    from bowhead_gateway.routes import documents, files
    from fastapi import FastAPI
    from test_document_http import SECRET, headers

    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "0")
    job_id = await setup_job(ctx, case)
    worker, calls = HostWorker(tmp_path / "worker"), []
    delivery = DocumentDeliveryStore(ctx.artifacts)
    await run_document(
        delivery=delivery, executor=worker, scope=SCOPE, job_id=job_id, invoke=transport([json.dumps(case[4])], calls), expected_image_id=IMAGE
    )
    draft = await delivery.get(scope=SCOPE, job_id=job_id)
    assert draft["state"] == "review", draft
    assert draft["draft"]["preview"]["page_count"] == 2
    app = FastAPI()
    app.state.settings = SimpleNamespace(auth_mode="prod")
    app.state.user_store = UserStore(ctx.path)
    app.state.session_store = ctx.sessions
    app.state.artifact_store = ctx.artifacts
    app.state.document_service = DocumentService(app.state)
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(files.router, prefix="/api/v1/files")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        base = f"/api/v1/documents/{job_id}"
        assert (await client.get(base + "/download/candidate.pptx", headers=headers())).status_code == 404
        for page in (1, 2):
            preview = await client.get(base + f"/preview/page-{page}.png", headers=headers())
            assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
        response = await client.post(
            base + "/confirm", headers=headers(), json={"command_id": "human-confirmation", "draft_digest": draft["draft_digest"]}
        )
        assert response.status_code == 200 and response.json()["state"] == "published"
        refreshed = await client.get("/api/v1/sessions/s_pipeline/documents", headers=headers())
        assert refreshed.status_code == 200 and refreshed.json()["jobs"][0]["draft_digest"] == draft["draft_digest"]
        for name in ("candidate.pptx", "candidate.pdf"):
            downloaded = await client.get(base + "/download/" + name, headers=headers())
            assert downloaded.status_code == 200
            assert hashlib.sha256(downloaded.content).hexdigest() == draft["draft"]["files"][name]["sha256"]
            generic = await client.get("/api/v1/files/" + draft["draft"]["files"][name]["artifact_id"], headers=headers())
            assert generic.status_code == 200 and generic.content == downloaded.content
    assert len(calls) == 1 and worker.runs == ["inventory", "apply_verify"]

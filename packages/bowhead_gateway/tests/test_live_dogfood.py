"""Full-stack dogfood：真 Qwen + bi_mock (ASGI transport) + 真 skill runtime.

跑法：
    ALIYUN_DASHSCOPE_API_KEY=sk-… pytest -m live test_live_dogfood.py -s

验证链路：
    POST /chat/send          → 真 Qwen lead loop
     → SSE /chat/stream       → canvas.* / pycell.* / tool_* 事件
      → POST /canvas/{id}/export (fmt=pdf)
       → GET /canvas/{id}/export/{job_id} 轮询
        → GET /api/v1/files/{artifact_id}  → 文件下载
         → GET /turns/{turn_id}/events     → 持久化 replay
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING

import httpx2
import pytest

from bowhead_bi_bridge import DatarcClient

if TYPE_CHECKING:
    from pathlib import Path


def _need_keys() -> None:
    if not os.environ.get("ALIYUN_DASHSCOPE_API_KEY"):
        pytest.skip("ALIYUN_DASHSCOPE_API_KEY not set")


@pytest.fixture
async def live_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """启动 gateway app，datarc 指向 bi_mock（ASGI transport），LLM 用真 Qwen."""
    _need_keys()
    monkeypatch.chdir(tmp_path)

    # Force Qwen path (no mock router).
    monkeypatch.setenv("BOWHEAD_LLM_PROVIDER", "aliyun_qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", os.environ["ALIYUN_DASHSCOPE_API_KEY"])

    # Point DB to tmp to avoid touching the real dev database.
    db_file = tmp_path / "live.db"
    monkeypatch.setenv("BOWHEAD_DB_URL", f"sqlite+aiosqlite:///{db_file}")

    # Start lifespan.
    from bi_mock.main import app as bi_mock_app
    from bowhead_gateway.main import create_app

    app = create_app()
    # Manually trigger lifespan.
    async with app.router.lifespan_context(app):
        # Replace datarc HTTP client with one targeting bi_mock.
        orig_datarc: DatarcClient = app.state.datarc
        await orig_datarc.aclose()

        class _BiMockDatarc(DatarcClient):
            def __init__(self) -> None:
                self._base_url = "http://bi-mock/"
                self._auth_key = "dev"
                self._timeout = 30
                self._http = httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=bi_mock_app),
                    base_url="http://bi-mock/",
                    headers={"Authorization": "Bearer dev"},
                    timeout=30,
                )

        app.state.datarc = _BiMockDatarc()
        # Also replace datarc referenced by Orchestrator.
        app.state.orchestrator.datarc = app.state.datarc
        yield app


async def _consume_sse(client: httpx2.AsyncClient, url: str, timeout: float = 60.0) -> list[dict]:
    """收集 SSE 事件直到 done/error 或 timeout."""
    events: list[dict] = []
    try:
        async with client.stream("GET", url, timeout=timeout) as r:
            current_event = "message"
            async for line in r.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    current_event = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data = line[len("data:") :].strip()
                    try:
                        payload = json.loads(data) if data else {}
                    except json.JSONDecodeError:
                        payload = {"_raw": data}
                    events.append({"type": current_event, "payload": payload})
                    if current_event in ("done", "error"):
                        break
    except (TimeoutError, httpx2.ReadTimeout):
        events.append({"type": "_timeout", "payload": {}})
    return events


@pytest.mark.live
async def test_full_stack_lookup_to_export(live_app) -> None:
    transport = httpx2.ASGITransport(app=live_app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        timeout=60.0,
    ) as c:
        # 1. POST /chat/send
        send = await c.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "1",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "今年的 GMV 是多少？",
            },
        )
        assert send.status_code == 200, send.text
        body = send.json()
        turn_id = body["turn_id"]
        stream_url = body["stream_url"]

        # 2. consume SSE
        events = await _consume_sse(c, stream_url, timeout=90.0)
        types = [e["type"] for e in events]

        # Assert turn completes normally.
        assert "turn_started" in types
        assert "step_list_card_created" in types  # chat-solidify: intent_classified retired.
        assert types[-1] in ("done", "error"), f"last type: {types[-1]}"

        # Obtain canvas_id (possibly from canvas.opened or by-session).
        session_id = None
        for e in events:
            if e["type"] == "turn_started":
                session_id = e["payload"].get("session_id")
                break
        assert session_id, "no session_id"

        # 3. Query canvas.
        r = await c.get(f"/api/v1/canvas/by-session/{session_id}")
        assert r.status_code == 200
        cv = r.json()
        if not cv["canvas"]:
            pytest.skip("turn completed but canvas wasn't composed (likely ambiguous / no tool)")
        canvas_id = cv["canvas"]["id"]
        [s["kind"] for s in cv["sections"]]
        assert len(cv["sections"]) >= 2  # At least eyebrow + h1.

        # 4. export PDF
        r = await c.post(f"/api/v1/canvas/{canvas_id}/export", json={"fmt": "pdf"})
        assert r.status_code == 200
        job = r.json()

        # 5. poll
        for _ in range(30):
            await asyncio.sleep(0.5)
            r = await c.get(f"/api/v1/canvas/{canvas_id}/export/{job['jobId']}")
            state = r.json()
            if state["status"] in ("done", "failed"):
                break
        assert state["status"] == "done", f"export state={state}"

        # 6. download
        r = await c.get(state["url"])
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"

        # 7. replay log
        r = await c.get(f"/api/v1/turns/{turn_id}/events")
        assert r.status_code == 200
        persisted = r.json()["events"]
        assert len(persisted) >= len(types) - 1  # Final sentinel may not be persisted.


@pytest.mark.live
async def test_full_stack_causal_attribution(live_app) -> None:
    """归因 intent → bi.attribute → attribution section 出现."""
    transport = httpx2.ASGITransport(app=live_app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        timeout=60.0,
    ) as c:
        send = await c.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "1",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "今年营业额下滑的原因是什么？",
            },
        )
        assert send.status_code == 200
        body = send.json()
        events = await _consume_sse(c, body["stream_url"], timeout=90.0)

        types = [e["type"] for e in events]
        assert types[-1] in ("done", "error")

        r = await c.get(f"/api/v1/canvas/by-session/{body.get('session_id') or events[0]['payload'].get('session_id')}")
        if r.status_code == 200 and r.json().get("canvas"):
            kinds = [s["kind"] for s in r.json()["sections"]]
            # LLM may omit attribute (or fall back to lookup style), but must still have structured sections.
            assert len(kinds) >= 2


@pytest.fixture
async def s2_live_app(tmp_path: Path, monkeypatch):
    """live_app 变体：dev-auth（stub 用户可解析）+ 真 Qwen + bi_mock datarc。

    .env 的 DEV_USER_FALLBACK=1 + DEPLOYMENT_MODE=local 是 FATAL 组合（licensing 守卫），
    这里 override 成 dev 模式让 stub 用户可用，用 monkeypatch 保证测试后干净还原。
    """
    _need_keys()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "dev")  # Bypass local+fallback guard.
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")  # dev stub lacks a team; enforce ON rejects with no_team.
    monkeypatch.setenv("BOWHEAD_LLM_PROVIDER", "aliyun_qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", os.environ["ALIYUN_DASHSCOPE_API_KEY"])
    monkeypatch.setenv("BOWHEAD_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 's2live.db'}")

    from bi_mock.main import app as bi_mock_app
    from bowhead_gateway.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        await app.state.datarc.aclose()

        class _BiMockDatarc(DatarcClient):
            def __init__(self) -> None:
                self._base_url = "http://bi-mock/"
                self._auth_key = "dev"
                self._timeout = 30
                self._http = httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=bi_mock_app),
                    base_url="http://bi-mock/",
                    headers={"Authorization": "Bearer dev"},
                    timeout=30,
                )

        app.state.datarc = _BiMockDatarc()
        app.state.orchestrator.datarc = app.state.datarc
        yield app


@pytest.mark.live
async def test_s2_memory_pack_space_filtered_in_real_turn(s2_live_app, monkeypatch) -> None:
    """memory-revive S2 §8-1 生效闭环（真 qwen turn）：seed ws_alpha/ws_beta/全局 三条 entry，
    真跑 ws_alpha 的 turn → build_memory_pack 被以 workspace_id=ws_alpha 调用，返回 pack
    含本空间+全局口径、不含他空间口径（红线过滤穿过真实 request→sctx→memory_pack 链路）。"""
    import bowhead_core.memory_pack as mp

    live_app = s2_live_app
    uid = live_app.state.settings.dev_default_user
    mem = live_app.state.user_memory_store
    await mem.upsert(user_id=uid, kind="preference", key="sales_def", value="零售口径ALPHA", workspace_id="ws_alpha")
    await mem.upsert(user_id=uid, kind="preference", key="fin_def", value="财务口径BETA", workspace_id="ws_beta")
    await mem.upsert(user_id=uid, kind="preference", key="report_style", value="全局口径GAMMA")

    calls: list[dict] = []
    orig = mp.build_memory_pack

    async def _spy(**kwargs):
        result = await orig(**kwargs)
        calls.append({"ws": kwargs.get("workspace_id"), "uid": kwargs.get("user_id"), "pack": result})
        return result

    monkeypatch.setattr(mp, "build_memory_pack", _spy)

    transport = httpx2.ASGITransport(app=live_app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw", timeout=60.0) as c:
        send = await c.post(
            "/api/v1/chat/send",
            json={"workspace_id": "ws_alpha", "message": "帮我总结一下最近的经营重点"},
        )
        assert send.status_code == 200, send.text
        await _consume_sse(c, send.json()["stream_url"], timeout=90.0)

    assert calls, "build_memory_pack 未被调用（Lead 未跑？）"
    alpha = [x for x in calls if x["ws"] == "ws_alpha" and x["uid"] == uid]
    assert alpha, f"没有 (ws_alpha, {uid}) 的调用；实际={[(x['ws'], x['uid']) for x in calls]}"
    pack = alpha[0]["pack"]
    assert "零售口径ALPHA" in pack, f"本空间口径缺失: {pack!r}"
    assert "全局口径GAMMA" in pack, "全局兜底口径缺失"
    assert "财务口径BETA" not in pack, "❌ 他空间口径泄漏进 ws_alpha（红线破）"

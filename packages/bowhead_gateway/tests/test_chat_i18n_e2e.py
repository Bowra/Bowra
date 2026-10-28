"""i18n v1 端到端集成测试.

验证 chat /send → orchestrator → Lead prompt 真注入 language directive.

不依赖真 LLM (Aliyun key) — 用 CapturingAdapter 记录 LLMRequest，
然后 inspect **模型看得见的全部文本**（`_visible()` = system + messages）。

⚠ 上下文工程 sprint 批 2-a 之后语言 directive 挂在 messages 尾而非 system，
只 inspect `req.system` 会把本文件的闸全变成假红。

覆盖关键路径:
- workspace_settings 含 i18n_v1=true → LanguageContext 生效
- user_profile.language=en-US → ui_language 解析为 en-US
- workspace_settings.default_data_language=zh-CN → data_language=zh-CN
- 最终请求（system + messages）含 §3.7 directive 段（output_language / data_language /
  week_start_day / glossary subset / tool 调用约束）
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx2
import pytest
from bi_mock.main import app as bi_mock_app
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.product_terms import default_catalog_path, load_catalog
from bowhead_core.session_store import SessionStore
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.workspace_settings_store import WorkspaceSettingsStore
from bowhead_gateway.main import create_app

from bowhead_bi_bridge import DatarcClient
from bowhead_core import (
    Orchestrator,
    OrchestratorConfig,
    build_default_registry,
    get_event_registry,
)
from bowhead_router import (
    AgentBinding,
    LLMRequest,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
)


@pytest.fixture(autouse=True)
def _instance_encryption_root(monkeypatch):
    """Deployments always set BOWHEAD_FERNET_KEY; since 9c6fe1654 every chat worker turn builds the
    physical-usage receipt cipher from it. Without .env in the checkout nothing else provides it."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")


@dataclass
class CapturingAdapter(MockAdapter):
    """MockAdapter 子类：记录每次 invoke 收到的 LLMRequest."""

    captured: list[LLMRequest] = field(default_factory=list)

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        self.captured.append(req)
        return await super().invoke(req)


def _visible(req: LLMRequest) -> str:
    """模型这一次**看得见的全部文本** = system + messages。

    ⚠ 上下文工程 sprint 批 2-a 之后，语言 directive 不再拼进 `system`，而是与 refs 清单
    一样挂在 **messages 末尾**（只进请求拷贝）。本文件的不变量是「模型看得见语言段」，
    不是「它在 system 字符串里」—— 只读 `req.system` 会把这些闸变成假红。
    """
    parts = [req.system or ""]
    parts.extend((getattr(m, "content", "") or "") for m in (req.messages or []))
    return "\n\n".join(x for x in parts if x)


@pytest.fixture
async def gateway_i18n_enabled(tmp_path):
    """构造一个 i18n_v1 启用的 gateway，workspace_id='test-ws'."""
    app = create_app()

    # ─── DB / stores ──────────────────────────────────────
    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}", target="core") == ("core",)
    db_path = str(core_path)
    user_settings_store = UserSettingsStore(db_path)
    await user_settings_store.init()
    workspace_settings_store = WorkspaceSettingsStore(db_path)
    await workspace_settings_store.init()

    # Seed workspace settings: i18n_v1 ON + zh-CN data.
    await workspace_settings_store.set("test-ws", "i18n_v1", "true")
    await workspace_settings_store.set("test-ws", "default_data_language", "zh-CN")
    await workspace_settings_store.set("test-ws", "week_start_day", "monday")

    # ─── datarc → bi_mock ─────────────────────────────────
    bi_transport = httpx2.ASGITransport(app=bi_mock_app)
    datarc = DatarcClient.__new__(DatarcClient)
    datarc._base_url = "http://bi-mock/"  # type: ignore[attr-defined]
    datarc._auth_key = "dev"  # type: ignore[attr-defined]
    datarc._timeout = 10  # type: ignore[attr-defined]
    datarc._http = httpx2.AsyncClient(  # type: ignore[attr-defined]
        transport=bi_transport,
        base_url=datarc._base_url,  # type: ignore[attr-defined]
        headers={"Authorization": "Bearer dev", "Content-Type": "application/json"},
        timeout=10,
    )

    # ─── CapturingAdapter ─────────────────────────────────
    adapter = CapturingAdapter(
        provider_name="p1",
        scripted_responses=[
            # Lead first turn: final answer directly, no tools (simplified path).
            LLMResponse(
                content="Test response from Lead.",
            ),
        ],
    )
    registry = ProviderRegistry()
    registry.load([ProviderConfig(name="p1", type="mock")])
    registry._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = Router(registry)
    for agent_type in ("lead", "modeler", "analyst", "echo", "judge"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("p1", "mock-model")))

    # ─── product terms catalog ────────────────────────────
    catalog_path = default_catalog_path()
    if not catalog_path.exists():
        pytest.skip(f"product_terms.yaml not at {catalog_path}")
    catalog = load_catalog(catalog_path)

    # ─── orchestrator ─────────────────────────────────────
    session_store = SessionStore(core_path)
    await session_store.initialize()
    turn_log_store = TurnLogStore(db_path)
    await turn_log_store.initialize()
    event_registry = get_event_registry()
    previous_log_sink = event_registry._default_log_sink  # noqa: SLF001
    event_registry.set_log_sink(OrderedTurnLogSink(turn_log_store))
    orchestrator = Orchestrator(
        router=router,
        datarc=datarc,
        tool_registry=build_default_registry(),
        event_registry=event_registry,
        config=OrchestratorConfig(),
    )
    orchestrator.dispatch_owner_id = "gateway_i18n_test"  # type: ignore[attr-defined]
    orchestrator.session_store = session_store
    orchestrator.product_terms_catalog = catalog  # type: ignore[attr-defined]

    # ─── Inject app.state, bypassing lifespan ───────────────────
    from bowhead_gateway.settings import Settings

    app.state.settings = Settings()
    app.state.datarc = datarc
    app.state.provider_registry = registry
    app.state.router = router
    app.state.orchestrator = orchestrator
    app.state.session_store = session_store
    app.state.turn_log_store = turn_log_store
    app.state.user_settings_store = user_settings_store
    app.state.workspace_settings_store = workspace_settings_store

    try:
        yield {
            "app": app,
            "adapter": adapter,
            "user_settings_store": user_settings_store,
            "workspace_settings_store": workspace_settings_store,
        }
    finally:
        event_registry.set_log_sink(previous_log_sink)
        await datarc.aclose()
        await registry.close_all()


async def _wait_for_done(client: httpx2.AsyncClient, stream_url: str, timeout: float = 15.0) -> list[dict]:
    """订阅 SSE 直到 done 或超时."""
    events: list[dict] = []
    try:
        async with asyncio.timeout(timeout):
            async with client.stream("GET", stream_url) as stream:
                async for line in stream.aiter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        events.append({"event": line[len("event:") :].strip()})
                    elif line.startswith("data:") and events:
                        try:
                            events[-1]["data"] = json.loads(line[len("data:") :].strip())
                        except json.JSONDecodeError:
                            events[-1]["data"] = line[len("data:") :].strip()
                    if events and events[-1]["event"] == "done":
                        return events
    except TimeoutError:
        pass
    return events


@pytest.mark.integration
async def test_i18n_directive_in_lead_prompt_when_enabled(gateway_i18n_enabled) -> None:
    """端到端：i18n_v1 开 + en-US user → Lead system prompt 含 directive 段."""
    app = gateway_i18n_enabled["app"]
    adapter = gateway_i18n_enabled["adapter"]

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "What is total revenue?",
                "user_profile": {"language": "en-US"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        await _wait_for_done(client, body["stream_url"], timeout=20.0)

    # Lead must be called at least once (first-turn system prompt).
    assert len(adapter.captured) >= 1, "MockAdapter 没收到任何调用"
    lead_req = adapter.captured[0]
    system = _visible(lead_req)

    # ─── Key assertions: directive actually injected ───────────
    # 1. Output-language segment (user profile=en-US).
    assert "## 输出语言与术语规范" in system, f"missing 输出语言 section: {system[-2000:]}"
    assert "output_language=en-US" in system, "directive missing output_language=en-US"
    # 2. Data language from workspace_settings.
    assert "data_language=zh-CN" in system, "directive missing data_language=zh-CN"
    # 3. Preserve proper nouns in original language.
    assert "保留原文不翻译" in system
    # 4. First day of week (monday).
    assert "monday" in system
    # 5. Glossary subset injection (Lead should see at least canvas / agent / dreaming).
    assert "canvas → Canvas" in system, "missing glossary entry canvas (en-US output)"
    assert "agent → Agent" in system
    # 6. Tool-call constraints: compute-agent-optimize batch 1-R6 **excludes Lead** (same as production lead.yaml;
    # no bi_ask_question/bi_fetch_rows; previously this segment appeared only in this conftest's stub Lead).
    # Peers with bi_query get it; see test_language_directive.TestToolConstraint.
    assert "Tool 调用约束" not in system
    assert "search_dimension_value" not in system  # Phantom tool retired.


@pytest.mark.integration
async def test_i18n_directive_skipped_when_flag_off(gateway_i18n_enabled) -> None:
    """关掉 workspace.i18n_v1 → directive 不注入（行为回退到现状）."""
    workspace_settings = gateway_i18n_enabled["workspace_settings_store"]
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]

    # Disable flag.
    await workspace_settings.set("test-ws", "i18n_v1", "false")

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "What is total revenue?",
                "user_profile": {"language": "en-US"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        await _wait_for_done(client, body["stream_url"], timeout=20.0)

    assert len(adapter.captured) >= 1
    system = _visible(adapter.captured[0])
    # Key assertion: directive segment absent.
    assert "## 输出语言与术语规范" not in system, "directive should be absent when flag off"
    assert "output_language=en-US" not in system
    assert "Tool 调用约束" not in system


@pytest.mark.integration
async def test_kill_switch_overrides_workspace_flag(
    gateway_i18n_enabled,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全局 kill switch (env BOWHEAD_I18N_V1_KILL=1) 即使 workspace 开了 flag 也 skip."""
    monkeypatch.setenv("BOWHEAD_I18N_V1_KILL", "1")
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "What is total revenue?",
                "user_profile": {"language": "en-US"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        await _wait_for_done(client, body["stream_url"], timeout=20.0)

    system = _visible(adapter.captured[0])
    assert "## 输出语言与术语规范" not in system, "kill switch should suppress directive"


# ─────────────────────────────────────────────────────────────
# Three-axis matrix: design doc §8 requires C1–C4 cases.
# ─────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.parametrize(
    "scenario,user_lang,workspace_data_lang,expected_output_lang,message",
    [
        # Chinese interface, source data, and answers.
        ("chinese-all-axes", "zh-CN", "zh-CN", "zh-CN", "今年的总营收是多少？"),
        # English interface and answers with Chinese source data.
        ("english-with-chinese-data", "en-US", "zh-CN", "en-US", "What is the total revenue this year?"),
        # English interface, source data, and answers.
        ("english-all-axes", "en-US", "en-US", "en-US", "What is the total revenue this year?"),
    ],
)
async def test_three_axis_matrix(
    gateway_i18n_enabled,
    scenario: str,
    user_lang: str,
    workspace_data_lang: str,
    expected_output_lang: str,
    message: str,
) -> None:
    """三轴矩阵 C1-C4: 不同 (user, data) 组合下 directive 内容正确."""
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]
    workspace_settings = gateway_i18n_enabled["workspace_settings_store"]

    # Interface and output are independent; a personal auto answer preference follows the
    # question (language-policy.md, AI answers), so each scenario asks in its own language.
    await gateway_i18n_enabled["user_settings_store"].set(app.state.settings.dev_default_user, "output_language", "auto")

    # Configure the source-data language.
    await workspace_settings.set("test-ws", "default_data_language", workspace_data_lang)

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": message,
                "user_profile": {"language": user_lang},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        await _wait_for_done(client, body["stream_url"], timeout=20.0)

    assert adapter.captured, f"{scenario}: adapter not invoked"
    system = _visible(adapter.captured[-1])
    assert f"output_language={expected_output_lang}" in system, f"{scenario} expected output_language={expected_output_lang} in directive"
    assert f"data_language={workspace_data_lang}" in system, f"{scenario} expected data_language={workspace_data_lang}"


@pytest.mark.integration
async def test_china_pm_overseas_report_scenario(gateway_i18n_enabled) -> None:
    """C5 特例：UI 中文 + output 英文（中国 PM 海外汇报场景）.

    user_settings 显式设 output_language=en-US 覆盖 ui_language.
    """
    user_settings = gateway_i18n_enabled["user_settings_store"]
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]
    workspace_settings = gateway_i18n_enabled["workspace_settings_store"]

    await workspace_settings.set("test-ws", "default_data_language", "zh-CN")
    # Use actual dev_default_user (chat.py uses this user_id).
    user_id = app.state.settings.dev_default_user
    await user_settings.set(user_id, "ui_language", "zh-CN")
    await user_settings.set(user_id, "output_language", "en-US")  # Explicit en.

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "今年的总营收是多少？",
                # Omit user_profile so backend uses user_settings.
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        await _wait_for_done(client, body["stream_url"], timeout=20.0)

    system = _visible(adapter.captured[-1])
    # Explicit user output override.
    assert "output_language=en-US" in system
    # Data follows workspace.
    assert "data_language=zh-CN" in system


@pytest.mark.skip(
    reason="Sprint θ Phase 8c-B: partial_answer event no longer fires at "
    "top-level (moved to SubagentRunner internal observability). Render "
    "pipeline still works (verified via system prompt assertion); end-to-end "
    "rendered-text via SSE needs gateway-level re-emit. See "
    "docs/sprints/unified-agentic-lead/phase-8c-b-test-adapt-strategy.md §4.",
)
@pytest.mark.integration
async def test_inline_numbers_directive_when_flag_on(gateway_i18n_enabled) -> None:
    """inline_numbers_v1 flag 开 → directive 含 syntax 段 + render 替换 [num:V:U]."""
    workspace_settings = gateway_i18n_enabled["workspace_settings_store"]
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]

    # Enable inline_numbers flag.
    await workspace_settings.set("test-ws", "inline_numbers_v1", "true")

    # Adapter simulates Lead's LLM output containing [num:V:U] syntax.
    from bowhead_router import LLMResponse

    adapter.scripted_responses = [
        LLMResponse(content="Q3 营收 [num:347100:CNY], 同比增长 [num:0.123:percent]"),
    ]
    adapter._script_index = 0

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "What was Q3 revenue?",
                "user_profile": {"language": "en-US"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        events = await _wait_for_done(client, body["stream_url"], timeout=20.0)

    # 1. Lead system prompt includes syntax-rule segment.
    assert len(adapter.captured) >= 1
    sys_prompt = _visible(adapter.captured[0])
    assert "## 数字写法" in sys_prompt
    assert "[num:VALUE:UNIT_TYPE]" in sys_prompt

    # 2. final_answer SSE rendered (en-US output uses K/M).
    partial = next(
        (e for e in events if e.get("event") == "partial_answer"),
        None,
    )
    assert partial is not None
    text = partial["data"]["text"]
    # Syntax replaced.
    assert "[num:" not in text
    # en-US compact + currency
    assert "347.1K" in text or "347" in text
    assert "12.3%" in text


@pytest.mark.skip(
    reason="Sprint θ Phase 8c-B: see test_inline_numbers_directive_when_flag_on skip reason — depends on partial_answer event re-emission.",
)
@pytest.mark.integration
async def test_inline_numbers_disabled_when_flag_off(gateway_i18n_enabled) -> None:
    """i18n_v1 开但 inline_numbers_v1 没开 → directive 不含 syntax 段; LLM 输出 syntax 不被 render."""
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]
    # Leave inline_numbers_v1 OFF (fixture enabled i18n_v1).

    from bowhead_router import LLMResponse

    adapter.scripted_responses = [
        LLMResponse(content="Q3 营收 [num:347100:CNY]"),
    ]
    adapter._script_index = 0

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "Q3 revenue?",
                "user_profile": {"language": "en-US"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        events = await _wait_for_done(client, body["stream_url"], timeout=20.0)

    # 1. Directive lacks syntax segment (flag OFF skips rules teaching LLM to write syntax).
    sys_prompt = _visible(adapter.captured[0])
    assert "## 数字写法" not in sys_prompt
    assert "[num:VALUE:UNIT_TYPE]" not in sys_prompt

    # 2. Always render final_answer, even with flag OFF, preventing syntax leaks to FE (M158 follow-up fix).
    # Flag controls teaching LLM the syntax; rendering is an always-running exit safety net.
    partial = next(
        (e for e in events if e.get("event") == "partial_answer"),
        None,
    )
    assert partial is not None
    text = partial["data"]["text"]
    # Syntax rendered (LLM may still emit it accidentally; exit must consume it).
    assert "[num:347100:CNY]" not in text


@pytest.mark.integration
async def test_mixed_language_history_carries_through(gateway_i18n_enabled) -> None:
    """An explicit auto answer preference follows UI switches across a session."""
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]

    await gateway_i18n_enabled["user_settings_store"].set(app.state.settings.dev_default_user, "output_language", "auto")

    # Script one response for each language.
    from bowhead_router import LLMResponse

    adapter.scripted_responses = [
        LLMResponse(content="第一轮中文回答"),
        LLMResponse(content="Second turn English answer"),
    ]
    adapter._script_index = 0

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        # Turn 1: zh
        r1 = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "今年 GMV 是多少",
                "user_profile": {"language": "zh-CN"},
            },
        )
        assert r1.status_code == 200
        body1 = r1.json()
        await _wait_for_done(client, body1["stream_url"], timeout=20.0)
        session_id = body1["session_id"]

        # Turn 2: en, same session.
        r2 = await client.post(
            "/api/v1/chat/send",
            json={
                "session_id": session_id,
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "What about Q4?",
                "user_profile": {"language": "en-US"},
            },
        )
        assert r2.status_code == 200
        body2 = r2.json()
        await _wait_for_done(client, body2["stream_url"], timeout=20.0)

    # Capture at least 2 invocations (turn 1 + turn 2).
    assert len(adapter.captured) >= 2

    # Turn 1 prompt: zh directive
    sys_t1 = _visible(adapter.captured[0])
    assert "output_language=zh-CN" in sys_t1

    # Turn 2 prompt: en directive (user switched).
    sys_t2 = _visible(adapter.captured[-1])
    assert "output_language=en-US" in sys_t2
    # B5 verification: no origin_language metadata in prompts (PO decision).
    assert "origin_language" not in sys_t2, "B5: 不应加 origin_language 元字段 — PO 决策 trust LLM 处理混语"


@pytest.mark.integration
async def test_output_language_only_triggers_user_level_fallback(gateway_i18n_enabled) -> None:
    """spawn-language review P2-3：flag 未设 + 用户只设 output_language（没动 ui_language）
    也算「显式选过语言」→ user-level fallback 启用，directive 注入。
    修前 user_picked_lang 只认 ui_language，output-only 用户拿不到 language_context，
    peer 的语言兜底注入整条失效。"""
    user_settings = gateway_i18n_enabled["user_settings_store"]
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]
    workspace_settings = gateway_i18n_enabled["workspace_settings_store"]

    await workspace_settings.delete("test-ws", "i18n_v1")  # Flag unset, not false.
    user_id = app.state.settings.dev_default_user
    await user_settings.set(user_id, "output_language", "en-US")  # Set only output.

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "今年的总营收是多少？",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        await _wait_for_done(client, body["stream_url"], timeout=20.0)

    system = _visible(adapter.captured[-1])
    assert "output_language=en-US" in system


@pytest.mark.integration
async def test_flag_unset_and_no_user_language_now_enabled(gateway_i18n_enabled) -> None:
    """flag 未设 + 用户什么语言都没选 → **启用**（2026-08-18 由 opt-in 翻成 opt-out）。

    ⚠ 这条原本断言的是「stays off」。**判据变了不是断言失效**：默认值本身就是被测行为，
    user 拍板界面语言不做 A/B（关着时后端退回 zh-CN 而前端只看 navigator.language，
    两边判据不同会让「英文浏览器 + 没点过切换器」的用户看到混排）。
    ★ 「显式关得掉」那一面没丢，由下面 `test_flag_false_...` 与
      `bowhead_core/tests/test_i18n_flag.py` 的两条正对照守着。
    """
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]
    workspace_settings = gateway_i18n_enabled["workspace_settings_store"]

    await workspace_settings.delete("test-ws", "i18n_v1")

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "test-ws",
                "member_uid": "aaa95ad475254d8e83566a13135e58fa",
                "message": "今年的总营收是多少？",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        await _wait_for_done(client, body["stream_url"], timeout=20.0)

    system = _visible(adapter.captured[-1])
    assert "output_language=" in system


# language-policy.md, AI answers: without a saved concrete answer preference, team default
# or lock, web answers follow the question's language; the interface keeps the browser.


async def _send(app, message: str, *, accept_language: str | None = None, session_id: str | None = None) -> dict:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        payload = {
            "workspace_id": "test-ws",
            "member_uid": "aaa95ad475254d8e83566a13135e58fa",
            "message": message,
        }
        if session_id is not None:
            payload["session_id"] = session_id
        resp = await client.post(
            "/api/v1/chat/send",
            headers={"Accept-Language": accept_language} if accept_language else {},
            json=payload,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        await _wait_for_done(client, body["stream_url"], timeout=20.0)
    return body


@pytest.mark.integration
@pytest.mark.parametrize(
    ("browser", "message", "expected"),
    [
        ("en-US,en;q=0.9", "今年各省的总收入是多少？", "zh-CN"),
        ("zh-CN,zh;q=0.9", "What was total revenue by province this year?", "en-US"),
    ],
    ids=["zh-question-en-browser", "en-question-zh-browser"],
)
async def test_unset_answer_preference_follows_the_question_language(gateway_i18n_enabled, browser: str, message: str, expected: str) -> None:
    adapter = gateway_i18n_enabled["adapter"]
    await _send(gateway_i18n_enabled["app"], message, accept_language=browser)

    assert f"output_language={expected}" in _visible(adapter.captured[-1])


@pytest.mark.integration
@pytest.mark.parametrize("reply", ["1", "ok", "SKU TOP10"])
async def test_bare_number_after_a_chinese_answer_stays_chinese(gateway_i18n_enabled, reply: str) -> None:
    """A reply without language evidence follows the session's previous answer, not the browser.

    language-policy.md: web chat uses the strict detector, so "ok" and "SKU TOP10" are no evidence.
    """
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]
    # Every model call answers in Chinese, so the persisted previous answer is Chinese.
    adapter.scripted_responses.clear()
    adapter.preset_response = "今年总收入为 1.2 亿元，北区最高。请问要细看哪个区域？1. 北区 2. 南区"
    first = await _send(app, "今年各区域的总收入是多少？", accept_language="en-US")
    assert "output_language=zh-CN" in _visible(adapter.captured[-1])

    await _send(app, reply, accept_language="en-US", session_id=first["session_id"])

    assert "output_language=zh-CN" in _visible(adapter.captured[-1])


@pytest.mark.integration
async def test_explicit_user_setting_still_beats_the_question_language(gateway_i18n_enabled) -> None:
    """An explicit Chinese answer preference remains effective for English input."""
    adapter = gateway_i18n_enabled["adapter"]
    app = gateway_i18n_enabled["app"]
    user_settings = gateway_i18n_enabled["user_settings_store"]
    await user_settings.set(app.state.settings.dev_default_user, "output_language", "zh-CN")

    await _send(app, "What was total revenue this year?")

    assert "output_language=zh-CN" in _visible(adapter.captured[-1])

"""Sprint θ Phase 8c-B test fixtures.

After 8c-B-1 (turn_runner full route to lead_orchestrator), tests that exercise
TurnRunner.run hit two new dependencies:

1. **PROFILES["lead"]** — Lead orchestrator checks this before dispatching
   (`lead_orchestrator.py:264`). Without it, Lead returns a skeleton fallback
   string and tests asserting content fail.

2. **SubagentRunner** — Lead delegates LLM tool-call loop to SubagentRunner.run.
   Tests want to control final_answer / tokens / events without booting a real
   LLM router or SubagentRunner.

This conftest provides:

- `_LeadProfileRegistered` autouse fixture — registers a minimal "lead" profile
  in `bowhead_core.subagent.PROFILES` for the duration of the test, removes after.
- `FakeSubagentRunner` — drop-in mock; tests inject via `orch.subagent_runner`.
  Returns pre-seeded SubagentResult objects from a queue; supports raise_exc for
  exception-path tests.

See `docs/sprints/unified-agentic-lead/phase-8c-b-test-adapt-strategy.md` for
the full adapt-not-delete strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult


def _use_plain_structlog_tracebacks() -> None:
    """测试里 structlog 的异常一律按纯文本 traceback 渲染。

    装了 rich 时 `ConsoleRenderer` 默认用 `rich_traceback`，并且逐帧展开局部变量；
    用例里的局部变量常是整张 SQLite 快照、事件列表或大 dict，一次 `log.exception`
    就能渲染很久，把一个普通失败拖成「挂死」。这里改掉类的默认值（覆盖网关
    lifespan 里后建的渲染器），并改写已配置的渲染器实例（覆盖 structlog 内置默认配置）。
    跨包不能共用辅助模块，core 与 gateway 的 conftest 各放一份。
    """
    import structlog
    from structlog.dev import ConsoleRenderer, plain_traceback

    rich_default = structlog.dev.default_exception_formatter
    defaults = ConsoleRenderer.__init__.__defaults__ or ()
    ConsoleRenderer.__init__.__defaults__ = tuple(plain_traceback if value is rich_default else value for value in defaults)
    for processor in structlog.get_config()["processors"]:
        if isinstance(processor, ConsoleRenderer):
            processor.exception_formatter = plain_traceback


_use_plain_structlog_tracebacks()


@pytest.fixture(autouse=True)
def _register_stub_lead_profile():
    """Insert minimal `lead` SubagentProfile so Lead orchestrator dispatches.

    Without this, `lead_orchestrator._lead_loop` short-circuits with skeleton
    fallback ("Lead profile not registered — Phase 8c-B wiring pending.").
    """
    pre_existing = "lead" in PROFILES
    if not pre_existing:
        # Whitelist covers tools used in TurnRunner integration tests that
        # exercise the real SubagentRunner (test_orchestrator vertical slice,
        # legacy approval/hook integration). FakeSubagentRunner-based tests
        # bypass this entirely.
        PROFILES["lead"] = SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            tool_whitelist=(
                "subagent_return",
                "bi_ask_question",
                "bi_attribute",
                "bi_fetch_rows",
                "bi_list_measures",
                "bi_list_dimensions",
                "kb_search",
                "kb_read",
                "skill_list",
                "skill_read",
                "skill_invoke",
                "canvas_read_section",
                "subagent_spawn",
                "clarification_ask",
            ),
            system_prompt="(stub Lead system prompt for tests)",
            max_iterations=8,
        )
    try:
        yield
    finally:
        if not pre_existing:
            PROFILES.pop("lead", None)


@dataclass
class FakeSubagentRunner:
    """Mock SubagentRunner for TurnRunner-path tests.

    Lead orchestrator calls `runner.run(profile_name="lead", ...)` and uses
    SubagentResult.final_answer / tokens / tool_invocations to build TurnResult.

    Tests seed `results` with pre-baked SubagentResult objects; each `.run()`
    pops one. If `raise_exc` is set, .run() raises that instead — exercises
    Lead's exception handling path (turn_failed/done emission).

    Tests that need to verify SubagentRunner-emitted events (tool_call,
    tool_result, etc.) at the parent_bus level can pass `emit_events`: a list of
    (event_type, payload) tuples that the runner emits on parent_bus before
    returning. This mirrors how real SubagentRunner publishes lifecycle events.

    Introspect `invocations` to assert Lead called us with right
    profile / task / extra_instructions.
    """

    results: list[SubagentResult] = field(default_factory=list)
    raise_exc: BaseException | None = None
    emit_events: list[tuple[str, dict]] = field(default_factory=list)
    invocations: list[dict[str, Any]] = field(default_factory=list)
    # canvas-summon: final-answer text fed to `stream_callback`. Default None streams nothing,
    # preserving existing cases byte for byte. Only set values trigger the callback; this is the sole entry
    # that makes Lead's StreamingDecomposer emit canvas sections, required to test the production output gate.
    stream_text: str | None = None

    async def run(
        self,
        *,
        profile_name: str,
        task: str,
        parent_turn_id: str,
        parent_bus: Any,
        parent_ctx: Any,
        extra_instructions: str | None = None,
        state: Any = None,
        stream_callback: Any = None,
        stream_on_tool_call: Any = None,
        **_kwargs: Any,
    ) -> SubagentResult:
        from bowhead_core.events import OrchestratorEvent

        self.invocations.append(
            {
                "profile_name": profile_name,
                "task": task,
                "parent_turn_id": parent_turn_id,
                "parent_ctx": parent_ctx,  # Peer-note wiring tests inspect session_store on ctx.
                "extra_instructions": extra_instructions,
                # Context-engineering sprint batch 3: also record history / history_rules passed by Lead
                # so wiring tests assert only Lead passes them and flag OFF omits them. Preserve all other kwargs too.
                **_kwargs,
            }
        )

        for event_type, payload in self.emit_events:
            await parent_bus.emit(
                OrchestratorEvent(
                    event_type=event_type,  # type: ignore[arg-type]
                    turn_id=parent_turn_id,
                    payload=payload,
                )
            )

        if self.stream_text and stream_callback is not None:
            for chunk in self.stream_text.splitlines(keepends=True):
                await stream_callback(chunk)

        if self.raise_exc is not None:
            raise self.raise_exc
        if self.results:
            return self.results.pop(0)
        return SubagentResult(
            profile=profile_name,
            subagent_id="sub_test",
            ok=True,
            final_answer="(default fake response)",
            tokens_input=10,
            tokens_output=5,
        )


@pytest.fixture
def fake_subagent_runner() -> FakeSubagentRunner:
    """Per-test FakeSubagentRunner; inject via `orch.subagent_runner = ...`."""
    return FakeSubagentRunner()


def _make_subagent_result(
    *,
    final_answer: str = "ok",
    ok: bool = True,
    tokens_input: int = 0,
    tokens_output: int = 0,
    tool_invocations: list | None = None,
    error: str | None = None,
) -> SubagentResult:
    """Convenience builder; tests don't need to import SubagentResult directly."""
    return SubagentResult(
        profile="lead",
        subagent_id="sub_test",
        ok=ok,
        final_answer=final_answer,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tool_invocations=list(tool_invocations or []),
        error=error,
    )


@pytest.fixture
def make_subagent_result():
    """Fixture wrapper so core tests do not import an ambiguous `conftest` module."""
    return _make_subagent_result


def _system_agent_capabilities() -> dict[str, frozenset[str]]:
    """真 system manifest 派生的能力表（agent-capability-routing 第 1 期）。

    ★ 为什么用**真注册表**而不是手写字典：`route_step_agent` 去名字化之后，
    「needs_corpus 的步必须去读得了上传文档的那个」这条防线不再由函数体内的常量保证，
    而是由「caller 传进来的能力表」保证。手写字典只能测函数，测不到**接线**——
    而接线正是新增的、也是唯一可能断的那一环（WP-2/WP-7 打下来的防线在这里）。
    """
    from bowhead_core.agents.capabilities import capabilities_by_agent
    from bowhead_core.agents.registry import AgentRegistry

    registry = AgentRegistry.bootstrap()
    return capabilities_by_agent(registry, None, registry.list_all())


@pytest.fixture
def system_agent_capabilities() -> dict[str, frozenset[str]]:
    return _system_agent_capabilities()


@pytest.fixture(scope="session")
def migrated_kb_files_snapshot(tmp_path_factory):
    """Pristine current ``files`` schema (Alembic head) for tests that build a KbFilesStore directly."""
    from bowhead_cli.migrate import upgrade

    directory = tmp_path_factory.mktemp("kb-files-schema")
    upgrade(f"sqlite+aiosqlite:///{directory / 'bowhead.db'}", target="files")
    return directory / "kb_files.db"


@pytest.fixture
def initialize_migrated_kb_files_database(migrated_kb_files_snapshot):
    """Copy the migrated kb_files baseline before a test's KbFilesStore initializes it."""
    import sqlite3

    def initialize(path):
        with sqlite3.connect(migrated_kb_files_snapshot) as source, sqlite3.connect(path) as destination:
            source.backup(destination)

    return initialize


@pytest.fixture(autouse=True)
def _drop_chroma_client_cache():
    """用例结束后清 chromadb 按路径缓存的客户端。

    `tmp_path_retention_policy = "failed"` 会在通过用例结束时删掉其 `tmp_path`，下一条
    参数化用例可能拿到同名路径；chromadb 的 `SharedSystemClient` 按路径缓存系统实例，
    复用到已删除目录上的旧连接会报 ``attempt to write a readonly database``。
    """
    yield
    import sys

    chroma_client = sys.modules.get("chromadb.api.shared_system_client")
    if chroma_client is not None:
        chroma_client.SharedSystemClient.clear_system_cache()


@pytest.fixture(autouse=True)
def _restore_canvas_export_job_store():
    """用例结束后还原 `canvas_export` 的模块级导出任务库路径与内存任务表。

    `init_export_jobs(path)` 把 `_db_path` 写成进程级全局；不还原的话，同 worker 上后续
    用例会拿到前一条用例的库路径。`tmp_path_retention_policy = "failed"` 删掉那条用例的
    目录后，就成了 ``unable to open database file``（`test_canvas_export_stub` 在全量里偶发）。
    """
    import sys

    module = sys.modules.get("bowhead_core.canvas_export")
    saved = (module._db_path, dict(module._jobs)) if module is not None else None
    yield
    module = sys.modules.get("bowhead_core.canvas_export")
    if module is None:
        return
    if saved is None:
        module._db_path = None
        module._jobs.clear()
    else:
        module._db_path = saved[0]
        module._jobs.clear()
        module._jobs.update(saved[1])


@pytest.fixture(autouse=True)
def _reset_shared_db_bootstrap():
    """用例结束后清 `_db._bootstrap_done`（按路径记「已设 WAL」）。

    `tmp_path_retention_policy = "failed"` 删掉通过用例的目录后，同名参数化用例会拿到
    同一路径上的**新**库文件；缓存不清则 `bootstrap_shared_db` 直接跳过，新库停在
    rollback journal（非 WAL），并发读写易报 ``database is locked``。
    连接池（`_read_pools`/`_shared_pools`）按 (st_dev, st_ino) 校验文件身份，换了文件会自动丢弃，
    不需要在这里复位。
    """
    yield
    import sys

    shared_db = sys.modules.get("bowhead_core._db")
    if shared_db is not None:
        shared_db._reset_bootstrap_for_test()

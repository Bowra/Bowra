"""Gateway test fixtures — Sprint θ Phase 8c-B.

Gateway integration tests bypass FastAPI lifespan (`app.state.X = ...` direct
assignment), so `bootstrap_multi_agent` never runs and `PROFILES["lead"]` is
not registered. Without it, `lead_orchestrator._lead_loop` short-circuits with
a skeleton response and SubagentRunner is never invoked — every integration
test fails immediately.

This autouse fixture mirrors the one in `bowhead_core/tests/conftest.py`:
inject a stub Lead SubagentProfile for the duration of each test, remove
afterward.
"""

from __future__ import annotations

import os

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

_AUTH_ENV_DEFAULTS = {"BOWHEAD_AUTH_MODE": "dev", "BOWHEAD_DEV_USER_FALLBACK": "1"}

#: Authentication environment at conftest **import time** (collection, before any test bodies run): developers' explicit
#: exported intent.
#:
#: ★ Not a session-scoped fixture: fixtures instantiate **lazily**, only when the first Gateway test
#: runs in this worker. testpaths=["packages"] collects alphabetically, placing
#: bowhead_cli before bowhead_gateway; any earlier environment pollution makes the captured baseline
#: polluted, then monkeypatch **pins it** across every later Gateway test,
#: worse than the original conditional guard (which at least restored 1 after pollution cleared).
#: Positive control verified: run a test leaking DEV_USER_FALLBACK=0, then
#: test_memory_routes_v3.py → 58 failures. Module-level capture passes the same combination.
_AUTH_ENV_BASELINE = {key: os.environ.get(key) for key in _AUTH_ENV_DEFAULTS}


@pytest.fixture(scope="module")
def community_installation_template(tmp_path_factory):
    from bowhead_cli.migrate import upgrade

    path = tmp_path_factory.mktemp("community-installation-template") / "bowhead.db"
    upgrade(f"sqlite:///{path}", target="core")
    return path


@pytest.fixture
async def completed_community_user_store(tmp_path, community_installation_template, monkeypatch):
    import sqlite3

    from bowhead_core.installation import community_setup_token, initialize_installation, setup_community_owner
    from bowhead_core.user_store import UserStore

    path = tmp_path / "bowhead.db"
    with sqlite3.connect(community_installation_template) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    identity = await initialize_installation(str(path), "community", admitted_fresh=True)
    await setup_community_owner(
        str(path),
        setup_token=community_setup_token(identity),
        team_name="HTTP Contract Tests",
        email="http-owner@example.test",
        password="SyntheticHttpOwner7!",
    )
    return UserStore(str(path))


@pytest.fixture(autouse=True)
def _default_explicit_dev_user_fallback(monkeypatch):
    """Make legacy local gateway tests state their historical auth assumption.

    Product code no longer treats missing JWT as a dev user unless both auth_mode
    and BOWHEAD_DEV_USER_FALLBACK opt in. Older gateway tests were written around
    that local-dev behavior, so the test environment enables it by default. Tests
    that exercise SaaS/prod auth set prod/0 in their own fixtures and override this.

    这里**无条件**设值，此前写的是 `if key not in os.environ`（同一提交里改掉）。
    条件式守卫的失效方式是静默的：只要进程 env 里已经有这个键（哪怕值是错的），
    它就什么都不做。实际发生过——`bowhead_cli` 的 composer-A rollback drill 在进程内
    跑时会留下 `BOWHEAD_DEV_USER_FALLBACK=0`，同一个 xdist worker 上后续所有 gateway
    用例于是全部 401，而 `make test` 每次红的集合还不一样（取决于调度）。drill 那边
    已改成还原 env（根因），这里按 `_AUTH_ENV_BASELINE` 无条件复位是第二道闸：
    开发者显式 export 的值仍然生效，跑中被别人改脏则每个用例开头都会被拨回来。
    """
    for key, fallback in _AUTH_ENV_DEFAULTS.items():
        baseline = _AUTH_ENV_BASELINE[key]
        monkeypatch.setenv(key, fallback if baseline is None else baseline)
    yield


@pytest.fixture(autouse=True)
def _register_stub_lead_profile():
    """Insert minimal `lead` SubagentProfile so Lead orchestrator dispatches.

    Whitelist mirrors the bowhead_core conftest — covers tools used in chat
    integration / i18n e2e tests that exercise the real SubagentRunner.
    """
    pre_existing = "lead" in PROFILES
    if not pre_existing:
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


def make_subagent_result(
    *,
    final_answer: str = "ok",
    ok: bool = True,
    tokens_input: int = 0,
    tokens_output: int = 0,
    tool_invocations: list | None = None,
    error: str | None = None,
) -> SubagentResult:
    """Mirror the core conftest helper for full-suite import-order safety."""
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


@pytest.fixture(scope="session")
def migrated_core_snapshot(tmp_path_factory):
    """Pristine current core schema for route fixtures that bypass lifespan."""
    from bowhead_cli.migrate import upgrade

    directory = tmp_path_factory.mktemp("usage-core-schema")
    upgrade(f"sqlite+aiosqlite:///{directory / 'bowhead.db'}", target="core")
    return directory / "bowhead.db"


@pytest.fixture
def initialize_migrated_core_database(migrated_core_snapshot):
    """Copy the real migrated baseline before a route fixture creates records."""
    import sqlite3

    def initialize(path):
        with sqlite3.connect(migrated_core_snapshot) as source, sqlite3.connect(path) as destination:
            source.backup(destination)

    return initialize


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


class AttributionMetricsProbe:
    """上传归属打点（`artifact.attribution`）的测试探针：真 `MetricsLog` 库 + **确定性** drain。

    打点是**脱离请求路径的后台任务**（见 `artifact_attribution` 模块 docstring ③），所以路由返回时
    行未必已写完。读之前必须 `drain(client)`：它在 `TestClient` 的 portal 事件循环里等队列排空，
    是确定性的（不 sleep、不轮询）。⇒ **要求 `with client:`**——否则每个请求各起一个即弃的事件循环，
    排队中的任务会被直接取消，读出来永远是 0，「flag 关」的对照也就成了空过。

    读取用同步 `sqlite3` 直读表：同步/异步测试都能用，不必在测试线程里再纠缠事件循环。
    （经 `MetricsLog.query_recent` + rollup 路由的完整链路由 `test_artifact_attribution_metrics.py` 单独覆盖。）
    """

    def __init__(self, db_path):
        import sqlite3

        from bowhead_core.metrics_log import MetricsLog

        self.db_path = str(db_path)
        self.log = MetricsLog(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_metrics_schema())

    def attach(self, app):
        """把 `MetricsLog` 挂到 app 上（`track_attribution` 从 `app.state.metrics_log` 取）。"""
        app.state.metrics_log = self.log
        return app

    def drain(self, client):
        from bowhead_gateway.artifact_attribution import drain_attribution_metrics

        assert client.portal is not None, "必须 `with client:` 进入上下文——否则后台打点任务会被随请求的事件循环一起取消"
        client.portal.call(drain_attribution_metrics, client.app)

    def _select(self, sql, *params):
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(sql, params).fetchall()

    def events(self):
        """全部 `artifact.attribution` 事件的 attrs，按 (route, reason, scope, source) 排序（顺序无关比较）。"""
        import json

        from bowhead_gateway.artifact_attribution import ATTRIBUTION_METRIC_NAME

        rows = self._select("SELECT attrs_json FROM metrics_event_log WHERE name=?", ATTRIBUTION_METRIC_NAME)
        attrs = [json.loads(r[0]) for r in rows]
        return sorted(attrs, key=lambda a: tuple(str(a.get(k, "")) for k in ("route", "reason", "scope", "source")))

    def user_ids(self):
        """落库行里的 user_id 列（本打点**不该**存任何用户标识）。"""
        return [r[0] for r in self._select("SELECT user_id FROM metrics_event_log")]


def _metrics_schema():
    from bowhead_core.metrics_log import SCHEMA

    return SCHEMA


@pytest.fixture
def fail_audit_event(monkeypatch):
    """让某个模块里指定名字的 `emit_audit(event, ...)` 抛错、其余照旧；返回 `install(module, event) -> restore`。

    用途：让请求在**紧贴打点之前的最后一步**失败——「成功之后才计数」的守卫就靠它证明：
    打点若被挪到比这更早的任何位置，这次失败也会被计进去。
    """

    def install(module, event):
        real = module.emit_audit

        def _maybe_boom(name, *args, **kwargs):
            if name == event:
                raise RuntimeError("audit sink down")
            return real(name, *args, **kwargs)

        monkeypatch.setattr(module, "emit_audit", _maybe_boom)
        return lambda: monkeypatch.setattr(module, "emit_audit", real)

    return install


@pytest.fixture
def attribution_metrics(tmp_path, monkeypatch):
    """归属打点探针。**显式把 flag 置为未设置**作起点：不让开发者/CI shell 里 export 的值渗进用例。"""
    monkeypatch.delenv("FEATURE_ARTIFACT_ATTRIBUTION_METRICS", raising=False)
    return AttributionMetricsProbe(tmp_path / "attribution_metrics.db")


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

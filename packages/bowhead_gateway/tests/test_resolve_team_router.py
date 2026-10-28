"""批 2-a C1 —— `resolve_team_router` 的五种 miss 必须各自可区分（tech design §3.1）.

为什么要区分：两层既有实现（`resolve_team_byo_overrides` / `TeamRouterRegistry.get`）
把四类原因折叠成同一个 None，照它们的返回值判，会让「配了但配坏了」的团队被告知
「未配置」，被指去重配一个已经配好的东西。

★ 初版这个文件把**错误行为钉死了**（实现层 review P0）：它假设
`migrate_legacy_config_to_v2` 对 legacy 只返 `{}`，于是断言「真实 v1 形状 →
LEGACY_CONFIG miss」。但该函数有成功路径，真实 legacy 行能迁出可用配置；而当时
那份 fixture 用了 `https://api.example.com/v1`，**恰好被 SSRF guard 拦掉**，是新旧
实现唯一一致的输入 —— 教科书级的「fixture 形状 ≠ 真实形状」。

现在的判据：`LEGACY_CONFIG` ＝「v1 且**迁不出来**」。可迁的 legacy 团队必须照常
拿到 router（下面 `test_migratable_legacy_still_gets_a_router` 是那条正对照）。
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.team_llm_config import SCHEMA_VERSION
from bowhead_gateway.routes._deps import RouterMiss, resolve_team_router


class _FakeUserStore:
    def __init__(self, config: Any, *, raises: bool = False) -> None:
        self._config = config
        self._raises = raises

    async def get_team_llm_config_with_version(self, team_id: str) -> tuple[Any, int]:
        if self._raises:
            raise RuntimeError("db down")
        return self._config, 7


class _FakeRegistry:
    def __init__(self, router: Any, *, raises: bool = False) -> None:
        self._router = router
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    async def get(self, team_id: str, llm_cfg: Any, version: int) -> Any:
        if self._raises:
            raise RuntimeError("build blew up")
        self.calls.append((team_id, version))
        return self._router


class _FakeRouter:
    def __init__(self, *, bound: tuple[str, ...] = ("lead", "echo", "analyst")) -> None:
        self._bound = bound

    def resolve(self, agent_type: str, profile: Any = None, user_pref: Any = None) -> Any:
        if agent_type not in self._bound:
            raise KeyError(f"No binding for agent_type={agent_type!r}")
        return object()


class _State:
    def __init__(self, user_store: Any, registry: Any) -> None:
        self.user_store = user_store
        self.team_router_registry = registry
        self.routing_config_loaded = None
        # Assembly placeholder: **no miss branch may return it** (§3.1 invariant).
        self.router = object()


def _v2(providers: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "providers": providers if providers is not None else {"p1": {"type": "openai_compatible"}},
        "agents": {},
    }


# ── Real legacy shape (v1): providers is a list ─────────────────────────────
# See team_llm_config.migrate_legacy_config_to_v2 comments: early BYO used
# providers[] + agent_routing. Both historical shapes share list-valued providers.
# Real migratable shape (key-only provider row documented in `team_llm_config.py:154-156`).
_LEGACY_MIGRATABLE = {"providers": [{"provider_name": "deepseek", "api_key": "sk-x", "role": "lead"}]}

# Unmigratable v1: providers is a list, but no item resolves to a provider name.
_LEGACY_UNMIGRATABLE = {"providers": [{"nonsense": 1}], "agent_routing": {}}


async def test_no_team_id_is_unconfigured() -> None:
    state = _State(_FakeUserStore(_v2()), _FakeRegistry(_FakeRouter()))
    router, miss = await resolve_team_router(state, None, agent_type="lead")
    assert router is None
    assert miss is RouterMiss.UNCONFIGURED


@pytest.mark.parametrize("config", [None, {}, {"providers": {}}, {"providers": []}])
async def test_empty_config_is_unconfigured(config: Any) -> None:
    state = _State(_FakeUserStore(config), _FakeRegistry(_FakeRouter()))
    router, miss = await resolve_team_router(state, "t_1", agent_type="lead")
    assert router is None
    assert miss is RouterMiss.UNCONFIGURED


async def test_migratable_legacy_still_gets_a_router() -> None:
    """★ 正对照：能迁的 legacy 团队必须照常拿到 router，**不得**被判成 miss。

    这是实现层 review 逮到的 P0：全仓其它每个 router 读者（chat / canvas /
    home_proposals / turn_service …）都在运行期迁移 legacy 行；本函数若把「是 v1」
    当 miss，配得好好的 legacy 团队会在 11 个功能面上被告知「模型未配置」而 chat
    照常工作。
    """
    fake = _FakeRouter()
    state = _State(_FakeUserStore(_LEGACY_MIGRATABLE), _FakeRegistry(fake))
    router, miss = await resolve_team_router(state, "t_1", agent_type="lead")
    assert miss is None, f"可迁移的 legacy 不该是 miss，实际 {miss}"
    assert router is fake


async def test_unmigratable_legacy_is_legacy_config() -> None:
    """迁不出来的 v1 才是 LEGACY_CONFIG——语义是「配了但用不了」，不是「是 v1」。"""
    state = _State(_FakeUserStore(_LEGACY_UNMIGRATABLE), _FakeRegistry(_FakeRouter()))
    router, miss = await resolve_team_router(state, "t_1", agent_type="lead")
    assert router is None
    assert miss is RouterMiss.LEGACY_CONFIG


async def test_valid_v2_but_registry_returns_none_is_build_failed() -> None:
    state = _State(_FakeUserStore(_v2()), _FakeRegistry(None))
    router, miss = await resolve_team_router(state, "t_1", agent_type="lead")
    assert router is None
    assert miss is RouterMiss.BUILD_FAILED


async def test_registry_raising_is_build_failed() -> None:
    state = _State(_FakeUserStore(_v2()), _FakeRegistry(None, raises=True))
    router, miss = await resolve_team_router(state, "t_1", agent_type="lead")
    assert router is None
    assert miss is RouterMiss.BUILD_FAILED


async def test_config_read_failure_is_resolve_error() -> None:
    state = _State(_FakeUserStore(None, raises=True), _FakeRegistry(_FakeRouter()))
    router, miss = await resolve_team_router(state, "t_1", agent_type="lead")
    assert router is None
    assert miss is RouterMiss.RESOLVE_ERROR


async def test_missing_registry_is_resolve_error() -> None:
    state = _State(_FakeUserStore(_v2()), None)
    router, miss = await resolve_team_router(state, "t_1", agent_type="lead")
    assert router is None
    assert miss is RouterMiss.RESOLVE_ERROR


async def test_unbound_agent_type_is_no_binding() -> None:
    """binding 预检收进解析器——canvas 与 home_proposals 各自已在做，不再第三次下放。"""
    state = _State(_FakeUserStore(_v2()), _FakeRegistry(_FakeRouter(bound=("lead",))))
    router, miss = await resolve_team_router(state, "t_1", agent_type="home_proposal")
    assert router is None
    assert miss is RouterMiss.NO_BINDING


async def test_agent_type_omitted_skips_binding_precheck() -> None:
    """agent_type 是可选 kwarg：不给则不预检，既有 4 个调用点行为逐字节不变。"""
    fake = _FakeRouter(bound=())
    state = _State(_FakeUserStore(_v2()), _FakeRegistry(fake))
    router, miss = await resolve_team_router(state, "t_1")
    assert router is fake
    assert miss is None


async def test_happy_path_returns_team_router() -> None:
    fake = _FakeRouter()
    registry = _FakeRegistry(fake)
    state = _State(_FakeUserStore(_v2()), registry)
    router, miss = await resolve_team_router(state, "t_1", agent_type="lead")
    assert router is fake
    assert miss is None
    assert registry.calls == [("t_1", 7)]


@pytest.mark.parametrize(
    ("config", "registry_router"),
    [
        (None, _FakeRouter()),
        (_LEGACY_UNMIGRATABLE, _FakeRouter()),
        (_v2(), None),
    ],
)
async def test_miss_never_returns_the_assembly_router(config: Any, registry_router: Any) -> None:
    """§3.1 的不变量：降级形态是「不调 LLM」，不是「调一个注定 refuse 的 router」。

    突变守卫——把任一 miss 分支改成 `return app_state.router, None` 必须让本条红。
    """
    state = _State(_FakeUserStore(config), _FakeRegistry(registry_router))
    router, _miss = await resolve_team_router(state, "t_1", agent_type="lead")
    assert router is None
    assert router is not state.router

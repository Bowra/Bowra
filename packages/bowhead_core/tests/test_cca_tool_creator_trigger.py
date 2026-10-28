"""cca P2.1 (C1) — tool_creator TRIGGER: emit contract + flag gating.

The trigger only emits a `cca.tool_create_open` SSE event carrying a URL hint (never a secret /
structured payload). The structured create + the credential go through the gateway P1 endpoints
from the FE card; that is NOT exercised here.
"""

from __future__ import annotations

import asyncio

from bowhead_core.tool_creator_trigger import (
    TOOL_NAME,
    _handler,
    _schema,
    register_tool_creator_trigger,
    tool_creator_enabled,
    tool_creator_proactive_enabled,
)
from bowhead_core.tool_registry import ToolExecutionContext


def _run(coro):
    return asyncio.run(coro)


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, e) -> None:
        self.events.append(e)


def _ctx(bus=None, turn_id="t1") -> ToolExecutionContext:
    ctx = ToolExecutionContext(
        workspace_id="",
        member_uid="",
        user_id="u",
        turn_id=turn_id,
        datarc=None,
        team_id="team_a",
        role="lead",  # type: ignore[arg-type]
    )
    if bus is not None:
        ctx.parent_bus = bus  # dynamic attr (how the orchestrator wires the per-turn bus)
    return ctx


def test_trigger_emits_open_event_with_trimmed_url_hint():
    bus = _Bus()
    res = _run(_handler({"spec_url": "  https://x/openapi.json  "}, _ctx(bus)))
    assert res["ok"] is True and res["opened"] is True
    assert len(bus.events) == 1
    ev = bus.events[0]
    assert ev.event_type == "cca.tool_create_open"
    assert ev.payload == {"spec_url_hint": "https://x/openapi.json", "kind_hint": None}  # trimmed
    # the trigger NEVER carries a secret or structured create state — only the hint
    # the payload stays a closed set of NON-secret hints — nothing structured, no credential
    assert set(ev.payload.keys()) == {"spec_url_hint", "kind_hint"}


def test_trigger_scrubs_credentials_from_spec_url_hint():
    # SEC-1: a spec URL can carry a credential (userinfo or ?api_key=). The hint rides the SSE
    # stream + the persisted turn log, so it MUST be scrubbed before emit (the §6.1 carve-out).
    bus = _Bus()
    _run(_handler({"spec_url": "https://user:pass@api.x.com/openapi.json?api_key=SECRET&format=json"}, _ctx(bus)))
    hint = bus.events[0].payload["spec_url_hint"]
    assert "SECRET" not in hint and "pass" not in hint and "?" not in hint
    assert hint == "https://api.x.com/openapi.json"  # SEC-1: reduced to scheme://host/path (query dropped)


def test_trigger_emits_null_hint_when_no_url():
    bus = _Bus()
    res = _run(_handler({}, _ctx(bus)))
    assert res["opened"] is True
    assert bus.events[0].payload == {"spec_url_hint": None, "kind_hint": None}


def test_trigger_without_bus_reports_not_opened():
    res = _run(_handler({"spec_url": "https://x"}, _ctx(bus=None)))
    assert res["ok"] is False and res["opened"] is False


class _FakeReg:
    def __init__(self) -> None:
        self.specs: list = []

    def register(self, spec) -> None:
        self.specs.append(spec)


def test_register_is_flag_gated_and_lead_only(monkeypatch):
    # flag OFF (default) → not registered
    monkeypatch.delenv("BOWHEAD_CCA_TOOL_CREATOR", raising=False)
    reg = _FakeReg()
    assert register_tool_creator_trigger(reg) is False
    assert reg.specs == [] and tool_creator_enabled() is False
    # flag ON → registered, advertised to the Lead only
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR", "1")
    reg2 = _FakeReg()
    assert register_tool_creator_trigger(reg2) is True
    assert len(reg2.specs) == 1
    spec = reg2.specs[0]
    assert spec.schema.name == TOOL_NAME
    assert spec.allowed_for_roles == frozenset({"lead"})
    assert spec.visible_to_llm is True


# external-capability-onboarding §3.2 — proactive-routing flag switches the description
# (review P1-4: OpenAPI-only wording missed turn t_8894ac3). Emit contract stays identical.


def test_description_is_openapi_only_when_proactive_disabled(monkeypatch):
    # default is ON now → the OFF path is opt-out via =0
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE", "0")
    assert tool_creator_proactive_enabled() is False
    desc = _schema().description
    assert "OpenAPI" in desc
    assert "MCP" not in desc  # Legacy wording never exposes the MCP lane.


def test_description_ends_out_mcp_and_cli_boundary_by_default(monkeypatch):
    # §3.2 proactive routing defaults ON (deploy-flip in code, no env needed)
    monkeypatch.delenv("BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE", raising=False)
    assert tool_creator_proactive_enabled() is True
    desc = _schema().description
    assert "MCP" in desc  # ends out the hosted-MCP lane
    assert "CLI" in desc  # honest boundary for local CLIs
    # emit / input contract does not vary WITH THE FLAG — both optional, neither required
    props = _schema().input_schema["properties"]
    assert set(props.keys()) == {"spec_url", "kind"}
    assert _schema().input_schema["required"] == []


def test_kind_hint_rides_through_and_is_whitelisted():
    """The Lead already classifies the address in its answer; `kind` is where it can SAY so, and the
    card then opens on the matching input mode instead of always defaulting to OpenAPI.

    Whitelisted, not trusted verbatim: it selects a UI tab, so an unexpected value must degrade to
    "unknown" (card keeps its default) rather than ride through."""
    for supplied, expected in (("mcp", "mcp"), ("openapi", "openapi"), ("nonsense", None), (None, None), (123, None)):
        bus = _Bus()
        args = {"spec_url": "https://x/mcp"} if supplied is None else {"spec_url": "https://x/mcp", "kind": supplied}
        res = _run(_handler(args, _ctx(bus)))
        assert res["opened"] is True
        assert bus.events[0].payload["kind_hint"] == expected, supplied


def test_onboarding_guidance_mandates_the_catalog_and_forbids_inventing_addresses():
    """116 dogfood twice showed both failure modes this block exists to stop.

    r1: the block never mentioned the one-click catalog, so the Lead improvised
    "paste https://docs.github.com/rest" — an HTML page that will not parse — while GitHub sat one
    click away. r2: after adding the catalog line it STILL led with OpenAPI, because the wording was
    so hedged ("don't name entries") that the model dropped the subject entirely; and it second-
    guessed a working address the user had already supplied, inventing another one.

    So both are pinned as requirements, not prose: the catalog must be mentioned FIRST, and inventing
    or doubting an address the user gave must be forbidden. Naming specific entries stays forbidden —
    the catalog is deployment- and entitlement-filtered, and the Lead cannot see it."""
    from bowhead_core.agents.prompts.lead import _TOOL_ONBOARD_GUIDANCE as g

    assert "one-click connectors" in g and "First ask the user" in g  # lead with the catalog, explicitly
    assert "do not name specific entries" in g  # ...but never claim what is in it
    assert "if the user already gave a URL, use it" in g  # don't re-guess a supplied address
    assert 'do not invent an "official doc URL" from memory' in g  # don't invent a docs URL
    # 116 dogfood: after this block grew, the Lead answered a Chinese question in English 4/4 on
    # this path while an untouched control turn stayed Chinese. Mechanism unproven (model, prompt
    # truncation and a malformed block were each ruled out), so the block states outright that it
    # is an operating rule and not a language switch — and that statement is pinned.
    assert "do not change the answer language" in g


def test_tool_result_message_carries_the_catalog_reminder():
    """The reminder rides the tool RESULT, not the Lead prompt.

    116 measured the tradeoff: an emphatic enough <tool_onboarding> block hit 4/4 on the catalog but
    flipped the answer language to English 3/4; compressing it back fixed language 4/4 and dropped
    the catalog to 3/4. This channel adds no prompt density — the model reads it immediately before
    composing — so it lifts the reminder without paying that price."""
    bus = _Bus()
    res = _run(_handler({}, _ctx(bus)))

    assert res["opened"] is True
    assert "一键连接器" in res["message"]
    assert "别点名具体条目" in res["message"]  # same honesty bound as the prompt block
    assert "密钥会加密保存" in res["message"]  # the pre-existing credential promise survives

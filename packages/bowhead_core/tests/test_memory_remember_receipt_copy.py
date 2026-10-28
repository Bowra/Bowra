"""What the model reads back from `remember`: no topic key, and the real UI location.

116 dogfood: the Lead answered 「已存为长期记忆（key：gmv_report_mom_yoy）…可在「记忆」页查看」.
The receipt echoed the key and named a page that does not exist; the model relayed both.
The fix withholds the key at the tool and names Settings › Memory, so every assertion
here reads the serialized receipt the model actually receives.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.memory_notes import MemoryNotesStore
from bowhead_core.memory_tools import _remember, register_memory_tools
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_core.user_memory import UserMemoryStore

from bowhead_core import memory_write_gate

_KEY = "gmv_report_mom_yoy"


@pytest.fixture
async def stores(tmp_path):
    memory = UserMemoryStore(tmp_path / "memory.db")
    await memory.initialize()
    notes = MemoryNotesStore(tmp_path / "notes.db")
    await notes.initialize()
    return memory, notes


def _ctx(memory, notes, lang: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws",
        member_uid="m",
        user_id="u1",
        turn_id="t1",
        datarc=None,
        memory_notes_store=notes,
        user_memory_store=memory,
        language_context=SimpleNamespace(output_language=lang),
    )


async def _remember_gmv(stores, lang: str) -> dict:
    memory, notes = stores
    return await _remember(
        {"kind": "preference", "key": _KEY, "text": "GMV 报告默认带环比和同比", "memory_type": "semantic"},
        _ctx(memory, notes, lang),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lang", "location"),
    [
        ("zh-CN", "「设置 › 记忆」"),
        ("en-US", "Settings › Memory"),
        ("de-DE", "„Einstellungen › Erinnerungen“"),
        ("fr-FR", "« Paramètres › Mémoire »"),
    ],
)
async def test_the_stored_receipt_names_settings_memory_and_omits_the_key(stores, lang, location) -> None:
    res = await _remember_gmv(stores, lang)
    wire = json.dumps(res, ensure_ascii=False)

    assert res["ok"] is True
    assert location in res["message"], res["message"]
    assert _KEY not in wire, "the topic key must not reach the model's receipt"
    assert "key" not in res


@pytest.mark.asyncio
async def test_the_zh_receipt_no_longer_names_the_nonexistent_memory_page(stores) -> None:
    res = await _remember_gmv(stores, "zh-CN")
    assert res["message"] == "已存为长期记忆，可在「设置 › 记忆」中查看或修改。"


@pytest.mark.asyncio
async def test_the_entry_id_stays_for_tool_calls_with_a_model_only_note(stores) -> None:
    """Same-turn `supersedes`, the turn receipt and the deliverable ledger read `id`; it stays, labelled internal."""
    res = await _remember_gmv(stores, "zh-CN")
    assert res["id"].startswith("mem_")
    assert "internal identifiers" in res["internal_note"]
    assert "Never show" in res["internal_note"]
    # The user-facing fields carry no identifiers.
    assert res["id"] not in res["message"] and res["id"] not in res["stored"]


@pytest.mark.asyncio
async def test_a_refusal_carries_no_internal_note(stores) -> None:
    """Negative control: a receipt without identifiers keeps its prior shape."""
    memory, notes = stores
    res = await _remember(
        {"kind": "preference", "text": "GMV 报告默认带环比和同比", "memory_type": "semantic"},
        _ctx(memory, notes, "zh-CN"),
    )
    assert res == {"ok": False, "error": "missing_key", "message": res["message"]}


def test_the_no_attribution_refusal_names_settings_memory() -> None:
    zh = memory_write_gate.check(value_text="GMV 报告默认带环比和同比", has_turn_id=False, actor_type=None, key=_KEY, lang="zh-CN")
    en = memory_write_gate.check(value_text="GMV reports include MoM and YoY", has_turn_id=False, actor_type=None, key=_KEY, lang="en-US")
    assert zh.reason == memory_write_gate.REASON_NO_ATTRIBUTION
    assert "「设置 › 记忆」" in zh.message and "记忆页" not in zh.message
    assert "Settings › Memory" in en.message


def test_the_registered_remember_description_names_settings_memory() -> None:
    registry = build_default_registry()
    register_memory_tools(registry)
    description = registry.get("remember").schema.description
    assert "「设置 › 记忆」" in description
    assert "『记忆』页" not in description
    assert "不要写进给用户的回答" in description


def test_the_lead_prompt_forbids_keys_and_names_the_location() -> None:
    prompt = build_lead_system_prompt("ws", facts=[])
    protocol = prompt[prompt.index("<memory_protocol>") : prompt.index("</memory_protocol>")]
    assert "「设置 › 记忆」" in protocol
    assert "never write memory keys or ids" in protocol

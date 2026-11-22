"""Turn visibility G2: steps name the four parts that reached users under their code names.

docs/sprints/turn-visibility-2026-11-20/tech-design.md §3.2. 116 (2026-11-06..20) showed
"view_file · full", "review_delivery", "agent_message" and "plan_propose" in step rows.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP
from bowhead_core.agents._generated.tool_display import TOOL_DISPLAY
from bowhead_core.subagent import _prettify_input_preview, _tool_header_zh, _view_file_step_label

_PARTS = ("view_file", "review_delivery", "agent_message", "plan_propose")


@pytest.mark.unit
@pytest.mark.parametrize("name", _PARTS)
def test_part_has_a_name_in_every_interface_language(name: str) -> None:
    entry = TOOL_DISPLAY[name]
    for language in ("zh", "en", "de", "fr"):
        label = entry[f"display_{language}"]
        assert label and label != name, f"{name} has no {language} name"
    assert entry["display_de"] != entry["display_en"], f"{name}: German must be written, not the English placeholder"
    assert _tool_header_zh(name) == entry["display_zh"], "the step header comes from the same table"


@pytest.mark.unit
@pytest.mark.parametrize("name", _PARTS)
def test_naming_a_part_does_not_change_who_may_call_it(name: str) -> None:
    """`role_binding: false` keeps the four out of the role map, as they were before they had names.

    A role-map entry would gate the registered ones by role and make them grantable natives for custom
    Agents; their own admission rules decide that.
    """
    from bowhead_core.subagent_authoring_catalog import NATIVE_ALLOWLIST

    assert name not in _PARADIGM_V2_ROLE_MAP
    assert name not in NATIVE_ALLOWLIST


@pytest.mark.unit
def test_view_file_label_never_shows_its_options() -> None:
    assert _prettify_input_preview("view_file", {"artifact_id": "art_0123456789ab", "detail": "full"}) == ""
    assert _prettify_input_preview("view_file", {"artifact_id": "art_0123456789ab", "detail": "sheet", "pages": [2]}) == ""


class _Store:
    def __init__(self, artifact: object | None = None, error: Exception | None = None) -> None:
        self.artifact, self.error, self.calls = artifact, error, []

    async def get(self, artifact_id: str, **kwargs: object) -> object | None:
        self.calls.append((artifact_id, kwargs))
        if self.error is not None:
            raise self.error
        return self.artifact


async def test_view_file_label_is_the_file_it_looks_at() -> None:
    store = _Store(SimpleNamespace(filename="dashboard.html"))
    ctx = SimpleNamespace(artifact_store=store, user_id="usr_1")
    assert await _view_file_step_label(ctx, {"artifact_id": "art_0123456789ab", "detail": "full"}) == "dashboard.html"
    assert store.calls == [("art_0123456789ab", {"check_created_by": "usr_1"})], "the lookup is scoped to the caller"


@pytest.mark.parametrize(
    ("ctx", "args"),
    [
        (SimpleNamespace(artifact_store=_Store(None), user_id="usr_1"), {"artifact_id": "art_0123456789ab"}),
        (SimpleNamespace(artifact_store=_Store(error=RuntimeError("busy")), user_id="usr_1"), {"artifact_id": "art_0123456789ab"}),
        (SimpleNamespace(user_id="usr_1"), {"artifact_id": "art_0123456789ab"}),
        (SimpleNamespace(artifact_store=_Store(SimpleNamespace(filename="x.html")), user_id="usr_1"), {"detail": "full"}),
    ],
    ids=["unreadable", "lookup fails", "no store", "no id"],
)
async def test_view_file_label_is_empty_when_the_file_cannot_be_named(ctx: object, args: dict) -> None:
    assert await _view_file_step_label(ctx, args) == ""

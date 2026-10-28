from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.skill_access import (
    render_access_filtered_skill_index,
    skill_visible_by_grants,
)


class _BoomAccessStore:
    async def explain_decision(self, *args, **kwargs):
        raise RuntimeError("policy store unavailable")


class _BoomCatalog:
    def list_specs(self):
        raise RuntimeError("catalog unavailable")


def _ctx():
    return SimpleNamespace(
        access_control_store=_BoomAccessStore(),
        access_context={"team_id": "team_a", "user_id": "u_1"},
    )


async def test_skill_visible_fails_closed_when_enforced(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")

    allowed = await skill_visible_by_grants(_ctx(), "forecasting")

    assert allowed is False


async def test_skill_visible_fails_open_in_shadow_mode(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "0")

    allowed = await skill_visible_by_grants(_ctx(), "forecasting")

    assert allowed is True


async def test_skill_index_filter_fails_closed_when_enforced(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")

    rendered = await render_access_filtered_skill_index(_ctx(), _BoomCatalog())

    assert rendered == ""

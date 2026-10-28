"""A nested workflow cannot replace its enclosing execution authority."""

import pytest
from bowhead_router.invocation import (
    begin_physical_invocation,
    enclosing_physical_guard_scope,
    physical_invocation_required,
    physical_invocation_scope,
)
from bowhead_router.types import TokenUsage

pytestmark = pytest.mark.unit


class Hook:
    def __init__(self, *, deny=False):
        self.deny = deny
        self.events = []

    async def begin(self, spec):
        self.events.append(("begin", spec.model))
        if self.deny:
            raise RuntimeError("child admission denied")
        return self

    async def confirmed_not_applied(self, **_kwargs):
        self.events.append("not_applied")
        return True

    async def complete(self, **_kwargs):
        self.events.append("completed")
        return True

    async def unknown(self, **_kwargs):
        self.events.append("unknown")


async def dispatch():
    return await begin_physical_invocation(provider="test", model="model", adapter_contract_key="test:v1", mode="invoke", request={"prompt": "test"})


async def test_parent_guard_survives_replacement_clearing_and_nested_duplicate_scope():
    parent, child = Hook(), Hook()
    with enclosing_physical_guard_scope(parent), enclosing_physical_guard_scope(parent):
        with physical_invocation_scope(None):
            assert physical_invocation_required()
            first = await dispatch()
            await first.complete(usage=TokenUsage(input_tokens=1), response_digest="a" * 64)
        with physical_invocation_scope(child):
            second = await dispatch()
            await second.unknown(reason_code="transport_interrupted")
    assert not physical_invocation_required()
    assert parent.events == [("begin", "model"), "completed", ("begin", "model"), "unknown"]
    assert child.events == [("begin", "model"), "unknown"]


async def test_child_denial_settles_parent_reservation_without_transport():
    parent = Hook()
    with (
        enclosing_physical_guard_scope(parent),
        physical_invocation_scope(Hook(deny=True)),
        pytest.raises(RuntimeError, match="child admission denied"),
    ):
        await dispatch()
    assert parent.events == [("begin", "model"), "not_applied"]

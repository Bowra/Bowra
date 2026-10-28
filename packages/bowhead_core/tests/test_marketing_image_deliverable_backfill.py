"""Image Capability deliverable backfill filters untrusted/non-output rows."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bowhead_core.subagent import _collect_image_deliverables


class _FakeArtifactStore:
    def __init__(self, kinds: dict[str, str]) -> None:
        self.kinds = kinds

    async def get(self, artifact_id: str) -> Any:
        kind = self.kinds.get(artifact_id)
        if kind is None:
            return None
        return SimpleNamespace(id=artifact_id, kind=kind)


def _inv(result: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(tool_name="image_generate", result=result)


def _tool_inv(tool_name: str, result: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool_name, result=result)


async def test_design_background_is_not_marketing_deliverable() -> None:
    invocations = [
        _inv(
            {
                "purpose": "design_background",
                "is_deliverable": False,
                "images": [
                    {
                        "artifact_id": "art_aaaaaaaaaaaa",
                        "purpose": "design_background",
                        "is_deliverable": False,
                    }
                ],
            }
        ),
        _inv(
            {
                "purpose": "deliverable",
                "is_deliverable": True,
                "images": [
                    {
                        "artifact_id": "art_bbbbbbbbbbbb",
                        "purpose": "deliverable",
                        "is_deliverable": True,
                    }
                ],
            }
        ),
    ]

    out = await _collect_image_deliverables(invocations)

    assert out == [{"kind": "image", "artifact_id": "art_bbbbbbbbbbbb"}]


async def test_legacy_image_result_checks_artifact_kind_when_store_available() -> None:
    invocations = [
        _inv({"images": [{"artifact_id": "art_aaaaaaaaaaaa"}]}),
        _inv({"images": [{"artifact_id": "art_bbbbbbbbbbbb"}]}),
    ]
    store = _FakeArtifactStore(
        {
            "art_aaaaaaaaaaaa": "tool_data",
            "art_bbbbbbbbbbbb": "deliverable",
        }
    )

    out = await _collect_image_deliverables(invocations, store)

    assert out == [{"kind": "image", "artifact_id": "art_bbbbbbbbbbbb"}]


async def test_llm_deliverable_claim_is_not_image_provenance() -> None:
    invocations = [
        _tool_inv(
            "subagent_return",
            {"deliverables": [{"kind": "image", "artifact_id": "art_forged000000"}]},
        ),
        _inv(
            {
                "is_deliverable": True,
                "images": [{"artifact_id": "art_authoritative0", "is_deliverable": True}],
            },
        ),
    ]

    out = await _collect_image_deliverables(invocations)

    assert out == [{"kind": "image", "artifact_id": "art_authoritative0"}]

"""User-visible Results artifact filtering policy.

Results navigation is a projection over existing artifacts.  Internal export
manifests and render inputs remain readable by their owning workflows, but they
must not consume a slot in the user-facing Results snapshot.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from bowhead_core.documents.retention import INTERNAL_PRODUCER
from bowhead_core.export_copy_contract import CURRENT_OUTPUT_PRODUCER_VALUES

CANVAS_EXPORT_MANIFEST_PRODUCER = "canvas_export_manifest_b1a_v1"
KB_BUNDLE_ROOT_PRODUCER = "kb_bundle_upload"
#: The sandbox also stores user-code stdout as an artifact (runner `_materialize_data_outputs`),
#: because the artifact is truth: inline stdout in the result is only a bounded model preview; full text exists
#: only as an artifact, which `answer_decomposer._sandbox_output_artifact_id` uses as the provenance
#: anchor for stdout numbers. Retain both; **do not skip persistence**.
#:
#: It is an internal result dependency, not a user deliverable: a clean 116 window measured 628
#: `sandbox-stdout.txt` files, 26.8% of session files (median 887 bytes; 12% below 100
#: bytes, mostly `done` / `start` / intermediate DataFrame dumps), while actual deliverables in those lists
#: were only 1.9%. Per this module's existing stance: retain readability, but omit from user-facing lists.
SANDBOX_STDOUT_PRODUCER = "python_sandbox_stdout_v1"

#: Single source of truth shared by the Python criterion and its SQL twin to prevent divergence
#: (SQL and Python here must be equivalent; see `results_internal_artifact_sql` docstring).
INTERNAL_PRODUCERS: tuple[str, ...] = (
    CANVAS_EXPORT_MANIFEST_PRODUCER,
    KB_BUNDLE_ROOT_PRODUCER,
    INTERNAL_PRODUCER,
    SANDBOX_STDOUT_PRODUCER,
)


def is_results_internal_artifact(
    artifact: Any,
    *,
    manifest_artifact_ids: Collection[str] = (),
) -> bool:
    """Return whether *artifact* is an internal dependency of a result.

    The decision uses persisted producer/role semantics only.  Filenames are
    deliberately excluded because user deliverables may legitimately share
    names with implementation artifacts.
    """

    artifact_id = str(getattr(artifact, "id", "") or "")
    if artifact_id and artifact_id in manifest_artifact_ids:
        return True

    producer_kind = str(getattr(artifact, "producer_kind", "") or "")
    if producer_kind in INTERNAL_PRODUCERS:
        return True

    # deck_render_inputs persists the structured Canvas input with this exact
    # semantic shape.  Historical rows were backfilled to kind=tool_data.
    return (
        str(getattr(artifact, "source", "") or "") == "canvas_export"
        and producer_kind == "canvas_export"
        and str(getattr(artifact, "kind", "") or "") == "tool_data"
    )


def results_internal_artifact_sql(alias: str = "a") -> tuple[str, tuple[str, ...]]:
    """SQL predicate equivalent to :func:`is_results_internal_artifact`.

    The manifest row-value subquery is deliberately uncorrelated: SQLite can
    materialize it once instead of scanning every artifact for every candidate.
    Filtering still happens ahead of LIMIT/OFFSET, so internal rows cannot
    silently truncate a Results snapshot page.
    """

    output_producers = tuple(sorted(CURRENT_OUTPUT_PRODUCER_VALUES))
    producer_placeholders = ", ".join("?" for _ in output_producers)
    internal_placeholders = ", ".join("?" for _ in INTERNAL_PRODUCERS)
    clause = f"""(
        COALESCE({alias}.producer_kind, '') IN ({internal_placeholders})
        OR (
            {alias}.source = 'canvas_export'
            AND COALESCE({alias}.producer_kind, '') = 'canvas_export'
            AND {alias}.kind = 'tool_data'
        )
        OR ({alias}.id, {alias}.created_by) IN (
            SELECT result_output.manifest_artifact_id, result_output.created_by
            FROM artifacts result_output
            WHERE result_output.manifest_artifact_id IS NOT NULL
              AND result_output.deleted_at IS NULL
              AND result_output.producer_kind IN ({producer_placeholders})
        )
    )"""
    return clause, (
        *INTERNAL_PRODUCERS,
        *output_producers,
    )


__all__ = [
    "CANVAS_EXPORT_MANIFEST_PRODUCER",
    "KB_BUNDLE_ROOT_PRODUCER",
    "INTERNAL_PRODUCERS",
    "SANDBOX_STDOUT_PRODUCER",
    "is_results_internal_artifact",
    "results_internal_artifact_sql",
]

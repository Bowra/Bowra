"""Trusted file ceilings applied before SQL projection, sorting and aggregation."""

import json


def append_source_filter(clauses: list[str], params: list, file_ids: tuple[str, ...] | None) -> None:
    if file_ids is not None:
        clauses.append("file_id IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(file_ids))

"""Static schema identity excludes only reviewed, nonunique KB field indexes."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_NAME = re.compile(r"idx_kb_(records|rows)_fld_([0-9a-f]{8})_([A-Za-z0-9_]{1,25})_v2")
_DDL = re.compile(
    r"CREATE\s+INDEX\s+(\w+)\s+ON\s+(kb_extracted_records|kb_extracted_rows)\s*"
    r"\(\s*workspace_id\s*,\s*doc_type_id\s*,\s*CAST\s*\(\s*json_extract\s*\(\s*fields_json\s*,\s*"
    r"'\$\.([^']+)\[0\]\.normalized\.amount'\s*\)\s+AS\s+REAL\s*\)\s*\)\s*"
    r"WHERE\s+soft_deleted_at\s+IS\s+NULL\s*;?",
    re.IGNORECASE,
)


def is_runtime_field_index(row: Sequence[str]) -> bool:
    """Match the actual field-index builder's name, owner and complete DDL."""
    kind, name, table, sql = row
    if kind != "index" or (identity := _NAME.fullmatch(name)) is None:
        return False
    ddl = _DDL.fullmatch(sql.strip())
    if ddl is None or ddl[1] != name or ddl[2] != table or table != f"kb_extracted_{identity[1]}":
        return False
    # Import lazily: the index builder depends on the database factory, whose
    # initialization also needs this digest helper.
    from bowhead_core.kb.extracted_record_store import _hash_field_name, _validate_field_name

    field = ddl[3]
    try:
        _validate_field_name(field)
    except ValueError:
        return False
    return name == f"idx_kb_{identity[1]}_fld_{identity[2]}_{_hash_field_name(field)}_v2"


def static_schema_digest(rows: Iterable[Sequence[str]]) -> str:
    """Hash every persistent object except the validated performance-only indexes."""
    static = [list(row) for row in rows if not is_runtime_field_index(row)]
    return hashlib.sha256(json.dumps(static, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

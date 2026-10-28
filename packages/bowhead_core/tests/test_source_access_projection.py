from __future__ import annotations

import copy

import pytest
from bowhead_core.source_access_projection import (
    SOURCE_ALIAS_PREFIX,
    SOURCE_NON_PRESENTATION_PATHS,
    SOURCE_PRESENTATION_PATHS,
    SourcePatchError,
    StaticSafeSourceMap,
    ViewerSourceAliasMap,
    bake_unavailable_prose_markers,
    collect_source_identities,
    merge_viewer_patch_preserving_provenance,
    project_section_for_static_export,
    project_sources_for_viewer,
    unregistered_source_like_paths,
)


def _aliases(*, workspace: str = "ws_1", report: str = "ses_1", secret: str = "root-secret") -> ViewerSourceAliasMap:
    return ViewerSourceAliasMap.from_root_secret(secret, workspace_id=workspace, report_scope_id=report)


def test_composed_view_settings_restore_canonical_columns_and_reject_data_forgery() -> None:
    child = {
        "kind": "table",
        "title": "Values",
        "subtitle": "Old subtitle",
        "row_count": 1,
        "data_artifact_id": "art_data",
        "columns": [
            {
                "key": "value",
                "header": "Value",
                "source_header": "Original value",
                "type": "numeric",
                "currency_code": "USD",
                "unit": "$",
                "display": {"decimal_places": 3, "decimal_mode": "fixed"},
            }
        ],
        "rows": [{"value": 12}],
    }
    root = {
        **copy.deepcopy(child),
        "views": [{"id": "first", "kind": "chart", "spec": copy.deepcopy(child)}, {"id": "second", "kind": "chart", "spec": copy.deepcopy(child)}],
        "layout": {"type": "stack", "children": [{"view": "first"}, {"view": "second"}]},
    }
    current = {"spec": root}
    aliases = _aliases()
    projected, _ = project_sources_for_viewer([{"id": "sec_1", "kind": "chart", **current}], [], alias_map=aliases, access_by_identity={})
    incoming = {"spec": copy.deepcopy(projected[0]["spec"])}
    changed = incoming["spec"]["views"][1]["spec"]
    changed["kind"] = "kpi"
    changed.pop("subtitle")
    merged = merge_viewer_patch_preserving_provenance(
        current, incoming, section_kind="chart", section_id="sec_1", alias_map=aliases, access_by_identity={}, allow_label_edit=False
    )
    assert merged["spec"]["views"][0] == current["spec"]["views"][0]
    assert merged["spec"]["views"][1]["spec"]["columns"] == child["columns"]
    assert "subtitle" not in merged["spec"]["views"][1]["spec"]
    changed["columns"][0]["display"]["decimal_places"] = 6
    with pytest.raises(SourcePatchError, match="observation metadata"):
        merge_viewer_patch_preserving_provenance(
            current, incoming, section_kind="chart", section_id="sec_1", alias_map=aliases, access_by_identity={}, allow_label_edit=False
        )
    changed["columns"][0]["display"]["decimal_places"] = 3
    changed["rows"][0]["value"] = 999
    with pytest.raises(SourcePatchError, match="observation metadata"):
        merge_viewer_patch_preserving_provenance(
            current, incoming, section_kind="chart", section_id="sec_1", alias_map=aliases, access_by_identity={}, allow_label_edit=False
        )


def test_alias_is_stable_scoped_opaque_and_invalid_slots_are_stable() -> None:
    one = _aliases()
    alias, raw = one.alias_for("art_secret_1", slot=("sec_1", "entries", 1))
    assert raw == "art_secret_1"
    assert alias.startswith(SOURCE_ALIAS_PREFIX)
    assert "art_secret_1" not in alias
    assert _aliases().alias_for("art_secret_1", slot=("other", "path", 2))[0] == alias
    assert _aliases(report="ses_2").alias_for("art_secret_1", slot=("sec_1", "entries", 1))[0] != alias
    assert _aliases(workspace="ws_2").alias_for("art_secret_1", slot=("sec_1", "entries", 1))[0] != alias
    invalid_a = one.alias_for("", slot=("sec_1", "entries", 1))[0]
    invalid_b = one.alias_for("", slot=("sec_1", "entries", 1))[0]
    assert invalid_a == invalid_b
    assert one.raw_for_alias(invalid_a) is None


def test_oversize_marker_uses_slot_alias_and_round_trips_without_hmacing_identity() -> None:
    raw = "x" * 513
    current = {"markdown": f"Evidence [ref:{raw}]"}
    aliases = _aliases()
    projected, _ = project_sources_for_viewer(
        [{"id": "sec_1", "kind": "narrative", **current}],
        [],
        alias_map=aliases,
        access_by_identity={},
    )
    safe_markdown = projected[0]["markdown"]
    assert raw not in safe_markdown
    assert SOURCE_ALIAS_PREFIX in safe_markdown

    merged = merge_viewer_patch_preserving_provenance(
        current,
        {"markdown": safe_markdown},
        section_kind="narrative",
        section_id="sec_1",
        alias_map=aliases,
        access_by_identity={},
        allow_label_edit=False,
    )
    assert merged == current


def test_all_registered_prose_fields_project_and_restore_marker_identities() -> None:
    current = {
        "title": "Title [ref:art_1]",
        "caption": "Caption [ref:art_1]",
        "exec_summary": "Summary [ref:art_1]",
        "owner": "Owner [ref:art_1]",
        "timeline": "Timeline [ref:art_1]",
    }
    aliases = _aliases()
    projected, _ = project_sources_for_viewer(
        [{"id": "sec_1", "kind": "recommendations", **current}],
        [],
        alias_map=aliases,
        access_by_identity={"art_1": False},
    )
    safe = {key: value for key, value in projected[0].items() if key not in {"id", "kind"}}
    assert "art_1" not in repr(safe)
    assert all(SOURCE_ALIAS_PREFIX in value for value in safe.values())
    assert (
        merge_viewer_patch_preserving_provenance(
            current,
            safe,
            section_kind="recommendations",
            section_id="sec_1",
            alias_map=aliases,
            access_by_identity={"art_1": False},
            allow_label_edit=False,
        )
        == current
    )


def test_noop_patch_bakes_unavailable_markers_but_preserves_accessible_provenance() -> None:
    canonical = "可访问 [ref:art_allowed]；旧数字 [num:5.2:亿元:src:tl_missing]；旧引用 [ref:chunk_missing]；普通标记 [num:7:%]。"

    normalized = bake_unavailable_prose_markers(
        canonical,
        {"art_allowed": True, "tl_missing": False, "chunk_missing": False},
    )

    assert normalized == "可访问 [ref:art_allowed]；旧数字 5.2亿元；旧引用；普通标记 7%。"


def test_projection_preserves_slots_but_removes_unavailable_business_detail() -> None:
    canonical = [
        {
            "id": "sec_sources",
            "kind": "sources",
            "entries": [
                {
                    "id": "art_allowed",
                    "artifact_id": "art_allowed",
                    "label": "Allowed revenue.csv",
                    "query": "revenue by region",
                    "preview_rows": [{"region": "North", "revenue": "42"}],
                    "preview_columns": ["region", "revenue"],
                },
                {
                    "id": "tl_denied",
                    "artifact_id": "tl_denied",
                    "label": "Secret customer table",
                    "query": "select customer_ssn",
                    "preview_rows": [{"ssn": "123"}],
                },
            ],
        },
        {
            "id": "sec_chart",
            "kind": "chart",
            "source_refs": ["art_allowed", "tl_denied"],
            "spec": {
                "title": "Revenue",
                "source_meta": {"id": "tl_denied", "tool": "bi_query", "query": "secret"},
            },
            "data_source": {"context_id": "private-connection", "context_name": "Private Fixture Connection", "tool": "datarc"},
        },
    ]
    original = copy.deepcopy(canonical)
    aliases = _aliases()
    projected, refs = project_sources_for_viewer(
        canonical,
        [
            {"id": "art_allowed", "artifact_id": "art_allowed", "label": "Allowed revenue.csv"},
            {"id": "tl_denied", "query": "secret"},
        ],
        alias_map=aliases,
        access_by_identity={"art_allowed": True, "tl_denied": False},
        safe_link_builder=lambda identity: {"safe_url": f"/safe/{identity}"},
    )

    assert canonical == original
    entries = projected[0]["entries"]
    assert len(entries) == 2
    assert entries[0]["access_state"] == "linked_accessible"
    assert entries[0]["safe_label"] == "Allowed revenue.csv"
    assert entries[0]["safe_url"] == "/safe/art_allowed"
    assert entries[1] == {
        "id": entries[1]["id"],
        "source_alias": entries[1]["id"],
        "access_state": "linked_unavailable",
        "generic_type": "query_result",
        "ordinal": 2,
    }
    serialized = repr(projected)
    assert "tl_denied" not in serialized
    assert "Secret customer" not in serialized
    assert "customer_ssn" not in serialized
    assert "private-connection" not in serialized
    assert projected[1]["source_refs"][0].startswith(SOURCE_ALIAS_PREFIX)
    assert len(refs) == 1 and refs[0]["access_state"] == "linked_accessible"


def test_identity_less_descriptive_entry_keeps_label_denied_entry_stays_stripped() -> None:
    """★116 dogfood 1.0.95 P3（t_7781d2970a27 批量标签形态）——成对钉：

    - identity-less 描述性条目（artifact_id=None 的 peer 自报，如「退款额/营业额/
      退款率」批量标签）没有可门禁对象，label/query 是报告作者文本 → unavailable
      投影**附带** safe_label/safe_query（FE 渲纯文本行，不再是误导性 unavailable）。
    - **有 identity 无权限**的条目照旧全剥（B1a 安全面一寸不让）。
    """
    canonical = [
        {
            "id": "sec_sources",
            "kind": "sources",
            "entries": [
                {
                    "artifact_id": None,
                    "label": "2026年7月各品牌退款额/营业额/退款率",
                    "tool": "bi_query",
                    "query": "2026年7月 各品牌 退款额 / 营业额 / 退款率",
                },
                {
                    "id": "tl_denied",
                    "artifact_id": "tl_denied",
                    "label": "Secret customer table",
                    "query": "select customer_ssn",
                },
            ],
        },
    ]
    projected, _ = project_sources_for_viewer(
        canonical,
        [],
        alias_map=_aliases(),
        access_by_identity={"tl_denied": False},
    )
    entries = projected[0]["entries"]
    desc, denied = entries[0], entries[1]
    assert desc["access_state"] == "descriptive"  # Distinct state, not confused with stale unavailable
    assert desc["safe_label"] == "2026年7月各品牌退款额/营业额/退款率"
    assert desc["safe_query"] == "2026年7月 各品牌 退款额 / 营业额 / 退款率"
    # Security control: identity present but permission absent → no label/query bytes exposed.
    assert "safe_label" not in denied and "safe_query" not in denied
    serialized = repr(projected)
    assert "Secret customer" not in serialized
    assert "customer_ssn" not in serialized


def test_collect_registry_covers_known_source_shapes() -> None:
    assert {(entry.section_kind, entry.path) for entry in SOURCE_PRESENTATION_PATHS} >= {
        ("*", ("source_refs", "*")),
        ("sources", ("entries", "*")),
        ("chart", ("spec", "source_meta")),
        ("chart", ("data_source",)),
        ("html_artifact", ("sources", "*")),
    }
    identities = collect_source_identities(
        [
            {"kind": "narrative", "source_refs": ["tl_1"]},
            {"kind": "sources", "entries": [{"artifact_id": "art_1"}]},
            {"kind": "chart", "spec": {"source_meta": {"id": "kb_1"}}},
        ],
        [{"id": "tl_2"}],
    )
    assert identities == ["tl_2", "tl_1", "art_1", "kb_1"]


def test_export_surface_source_named_fields_are_registered_or_explicitly_non_presentation() -> None:
    from bowhead_core.export_snapshot import _KIND_PAYLOAD_FIELDS

    registered = {(entry.section_kind, entry.path[0]) for entry in SOURCE_PRESENTATION_PATHS if entry.path}
    non_presentation = {(kind, path[0]) for kind, path in SOURCE_NON_PRESENTATION_PATHS if path}
    unknown = {
        (kind, field)
        for kind, fields in _KIND_PAYLOAD_FIELDS.items()
        for field in fields
        if "source" in field.lower() and (kind, field) not in registered and ("*", field) not in registered and (kind, field) not in non_presentation
    }
    assert unknown == set()


def test_unknown_source_like_field_is_redacted_from_viewer_and_static_export() -> None:
    raw = {
        "id": "sec_1",
        "kind": "narrative",
        "markdown": "Safe prose",
        "source_debug": {"artifact_id": "art_secret", "query": "private"},
    }
    assert unregistered_source_like_paths("narrative", raw) == (("source_debug",),)

    viewer, _ = project_sources_for_viewer(
        [raw],
        [],
        alias_map=_aliases(),
        access_by_identity={},
    )
    assert viewer[0]["markdown"] == "Safe prose"
    assert "source_debug" not in viewer[0]
    assert "art_secret" not in repr(viewer[0])

    static, _ = project_section_for_static_export(
        section_id="sec_1",
        section_kind="narrative",
        payload=raw,
        source_refs=[],
        source_map=StaticSafeSourceMap(),
    )
    assert static["markdown"] == "Safe prose"
    assert "source_debug" not in static
    assert "art_secret" not in repr(static)


def test_unknown_source_like_viewer_patch_is_rejected() -> None:
    with pytest.raises(SourcePatchError) as exc:
        merge_viewer_patch_preserving_provenance(
            {"markdown": "Before"},
            {"markdown": "After", "source_debug": {"artifact_id": "art_attacker"}},
            section_kind="narrative",
            section_id="sec_1",
            alias_map=_aliases(),
            access_by_identity={},
            allow_label_edit=False,
        )
    assert exc.value.code == "source_identity_injection"


def test_sanitized_noop_and_normal_edit_restore_canonical_provenance() -> None:
    current = {
        "label": "Sources",
        "entries": [
            {"id": "art_1", "artifact_id": "art_1", "label": "Revenue", "query": "secret query"},
            {"id": "tl_2", "label": "Denied", "query": "very secret"},
        ],
        "source_refs": ["art_1", "tl_2"],
    }
    aliases = _aliases()
    projected, _ = project_sources_for_viewer(
        [{"id": "sec_1", "kind": "sources", **current}],
        [],
        alias_map=aliases,
        access_by_identity={"art_1": True, "tl_2": False},
    )
    incoming = {key: value for key, value in projected[0].items() if key not in {"id", "kind"}}
    merged = merge_viewer_patch_preserving_provenance(
        current,
        incoming,
        section_kind="sources",
        section_id="sec_1",
        alias_map=aliases,
        access_by_identity={"art_1": True, "tl_2": False},
        allow_label_edit=True,
    )
    assert merged == current

    incoming["entries"][0]["safe_label"] = "Revenue reviewed"
    incoming["entries"][1]["safe_label"] = "Attacker overwrite"
    edited = merge_viewer_patch_preserving_provenance(
        current,
        incoming,
        section_kind="sources",
        section_id="sec_1",
        alias_map=aliases,
        access_by_identity={"art_1": True, "tl_2": False},
        allow_label_edit=True,
    )
    assert edited["entries"][0]["label"] == "Revenue reviewed"
    assert edited["entries"][1]["label"] == "Denied"
    assert edited["entries"][0]["artifact_id"] == "art_1"
    assert edited["source_refs"] == ["art_1", "tl_2"]


@pytest.mark.parametrize("bad", ["art_attacker", SOURCE_ALIAS_PREFIX + "stale"])
def test_patch_rejects_raw_or_unknown_source_identity(bad: str) -> None:
    aliases = _aliases()
    aliases.alias_for("art_1", slot=("sec_1", "source_refs", 1))
    with pytest.raises(SourcePatchError) as exc:
        merge_viewer_patch_preserving_provenance(
            {"source_refs": ["art_1"]},
            {"source_refs": [bad]},
            section_kind="narrative",
            section_id="sec_1",
            alias_map=aliases,
            access_by_identity={"art_1": True},
            allow_label_edit=False,
        )
    assert exc.value.code in {"source_identity_injection", "source_alias_stale"}


def test_chart_normal_edit_does_not_erase_source_meta_or_data_source() -> None:
    current = {
        "spec": {"title": "Before", "rows": [{"x": 1}], "source_meta": {"id": "art_1", "query": "secret"}},
        "data_source": {"context_id": "private", "context_name": "Prod", "tool": "datarc"},
    }
    aliases = _aliases()
    source_alias = aliases.alias_for("art_1", slot=("sec_chart", "spec.source_meta", 1))[0]
    merged = merge_viewer_patch_preserving_provenance(
        current,
        {"spec": {"title": "After", "rows": [{"x": 2}], "source_meta": {"id": source_alias}}},
        section_kind="chart",
        section_id="sec_chart",
        alias_map=aliases,
        access_by_identity={"art_1": True},
        allow_label_edit=False,
    )
    assert merged["spec"]["title"] == "After"
    assert merged["spec"]["source_meta"] == current["spec"]["source_meta"]
    assert merged["data_source"] == current["data_source"]


def test_chart_rejects_forged_source_alias() -> None:
    aliases = _aliases()
    aliases.alias_for("art_1", slot=("sec_chart", "spec.source_meta", 1))
    with pytest.raises(SourcePatchError) as exc:
        merge_viewer_patch_preserving_provenance(
            {"spec": {"title": "Before", "source_meta": {"id": "art_1"}}},
            {"spec": {"title": "After", "source_meta": {"id": SOURCE_ALIAS_PREFIX + "fake"}}},
            section_kind="chart",
            section_id="sec_chart",
            alias_map=aliases,
            access_by_identity={"art_1": True},
            allow_label_edit=False,
        )
    assert exc.value.code == "source_alias_stale"


def test_sources_rejects_extra_raw_identity_carrier() -> None:
    aliases = _aliases()
    alias = aliases.alias_for("art_1", slot=("sec_1", "entries", 1))[0]
    with pytest.raises(SourcePatchError) as exc:
        merge_viewer_patch_preserving_provenance(
            {"entries": [{"id": "art_1", "artifact_id": "art_1", "label": "Revenue"}]},
            {"entries": [{"id": alias, "source_alias": alias, "artifact_id": "art_attacker"}]},
            section_kind="sources",
            section_id="sec_1",
            alias_map=aliases,
            access_by_identity={"art_1": True},
            allow_label_edit=True,
        )
    assert exc.value.code == "source_identity_injection"


def test_static_export_projection_keeps_only_ordinals_and_generic_types() -> None:
    source_map = StaticSafeSourceMap()
    payload, refs = project_section_for_static_export(
        section_id="sec_sources",
        section_kind="sources",
        payload={
            "label": "Sources",
            "entries": [
                {
                    "id": "art_private",
                    "artifact_id": "art_private",
                    "label": "Board revenue.xlsx",
                    "query": "select confidential_margin",
                    "preview_rows": [{"margin": "secret"}],
                },
                {"id": "tl_private", "tool": "bi_query", "label": "Customer ledger"},
            ],
            "markdown": "See [ref:art_private] and [num:42:count:src:tl_private]",
        },
        source_refs=["art_private", "tl_private"],
        source_map=source_map,
    )
    serialized = repr((payload, refs))
    assert "art_private" not in serialized
    assert "tl_private" not in serialized
    assert "Board revenue" not in serialized
    assert "confidential_margin" not in serialized
    assert "Customer ledger" not in serialized
    assert refs == ["source-ordinal:1", "source-ordinal:2"]
    assert payload["entries"] == [
        {"source_ref": "source-ordinal:1", "generic_type": "generated_artifact", "ordinal": 1},
        {"source_ref": "source-ordinal:2", "generic_type": "query_result", "ordinal": 2},
    ]


def test_static_export_projection_reuses_token_across_sections_without_persisting_map() -> None:
    source_map = StaticSafeSourceMap()
    first, refs = project_section_for_static_export(
        section_id="sec_1",
        section_kind="narrative",
        payload={"markdown": "[ref:kb_private]"},
        source_refs=["kb_private"],
        source_map=source_map,
    )
    second, second_refs = project_section_for_static_export(
        section_id="sec_2",
        section_kind="chart",
        payload={"spec": {"source_meta": {"id": "kb_private", "kb_filename": "Secret plan.pdf"}}},
        source_refs=[],
        source_map=source_map,
    )
    assert refs == ["source-ordinal:1"]
    assert second_refs == []
    assert "source-ordinal:1" in first["markdown"]
    assert second["spec"]["source_meta"]["source_ref"] == "source-ordinal:1"
    assert "Secret plan" not in repr(second)


def test_static_export_projection_normalizes_object_source_refs() -> None:
    source_map = StaticSafeSourceMap()
    payload, refs = project_section_for_static_export(
        section_id="sec_1",
        section_kind="narrative",
        payload={"markdown": "[ref:art_private]"},
        source_refs=[{"artifact_id": "art_private", "label": "Private.xlsx"}],
        source_map=source_map,
    )
    assert refs == ["source-ordinal:1"]
    assert payload["markdown"] == "[ref:source-ordinal:1]"


def test_chart_projection_does_not_restore_raw_markers_when_replacing_source_meta() -> None:
    raw = {
        "id": "sec_chart",
        "kind": "chart",
        "spec": {
            "title": "Revenue [ref:art_private]",
            "source_meta": {"artifact_id": "art_private", "query": "secret"},
        },
    }
    aliases = _aliases()
    viewer, _ = project_sources_for_viewer(
        [raw],
        [],
        alias_map=aliases,
        access_by_identity={"art_private": False},
    )
    assert "art_private" not in repr(viewer)
    assert SOURCE_ALIAS_PREFIX in viewer[0]["spec"]["title"]

    static, _ = project_section_for_static_export(
        section_id="sec_chart",
        section_kind="chart",
        payload=raw,
        source_refs=[],
        source_map=StaticSafeSourceMap(),
    )
    assert "art_private" not in repr(static)
    assert "source-ordinal:1" in static["spec"]["title"]


def test_html_source_ordinals_are_normalized_on_view_and_static_export() -> None:
    canonical = {
        "id": "html_1",
        "kind": "html_artifact",
        "artifact_id": "art_html",
        "sources": [2, "art_secret", True, 2.0, 3, 3],
    }
    projected, _ = project_sources_for_viewer(
        [canonical],
        [],
        alias_map=_aliases(),
        access_by_identity={},
    )
    assert projected[0]["sources"] == [2, 3]
    assert "art_secret" not in repr(projected)

    static, _ = project_section_for_static_export(
        section_id="html_1",
        section_kind="html_artifact",
        payload=canonical,
        source_refs=(),
        source_map=StaticSafeSourceMap(),
    )
    assert static["sources"] == [2, 3]
    assert "art_secret" not in repr(static)


def test_html_source_ordinal_patch_round_trips_but_rejects_injection() -> None:
    canonical = {"artifact_id": "art_html", "sources": [2, 2.0, 3]}
    aliases = _aliases()
    merged = merge_viewer_patch_preserving_provenance(
        canonical,
        {"sources": [2, 3]},
        section_kind="html_artifact",
        section_id="html_1",
        alias_map=aliases,
        access_by_identity={},
        allow_label_edit=False,
    )
    assert merged == canonical

    with pytest.raises(SourcePatchError) as exc:
        merge_viewer_patch_preserving_provenance(
            canonical,
            {"sources": [2, "art_secret"]},
            section_kind="html_artifact",
            section_id="html_1",
            alias_map=aliases,
            access_by_identity={},
            allow_label_edit=False,
        )
    assert exc.value.code == "source_shape_conflict"


# ─── Batch 0 0-D (chart-table-ux tech-design §1.4): chart source-line [N] points to actual ordinal ───


def _chart_section(section_id: str, artifact_id: str) -> dict:
    return {
        "id": section_id,
        "kind": "chart",
        "spec": {
            "kind": "bar",
            "title": f"chart-{section_id}",
            "source_meta": {
                "id": artifact_id,
                "tool": "bi_fetch_rows",
                "artifact_id": artifact_id,
            },
        },
    }


def _sources_section(*artifact_ids: str, superseded: bool = False) -> dict:
    out = {
        "id": "sec_sources",
        "kind": "sources",
        "entries": [{"id": aid, "artifact_id": aid, "label": f"查询 {i}", "tool": "bi_query"} for i, aid in enumerate(artifact_ids, start=1)],
    }
    if superseded:
        out["superseded"] = True
    return out


def test_chart_source_ordinal_matches_sources_entry_position() -> None:
    """能对上来源清单 → 下发该 entry 的真实序号（不再全 [1]）。"""
    projected, _ = project_sources_for_viewer(
        [
            _sources_section("art_a", "art_b", "art_c"),
            _chart_section("sec_chart_1", "art_b"),
            _chart_section("sec_chart_2", "art_c"),
        ],
        [],
        alias_map=_aliases(),
        access_by_identity={"art_a": True, "art_b": True, "art_c": True},
    )
    charts = [s for s in projected if s["kind"] == "chart"]
    assert charts[0]["spec"]["source_meta"]["ordinal"] == 2
    assert charts[1]["spec"]["source_meta"]["ordinal"] == 3


def test_chart_source_ordinal_absent_when_artifact_not_in_sources() -> None:
    """★负对照：对不上清单 → 不下发 ordinal（FE 显来源名，绝不渲假 [1]）。"""
    projected, _ = project_sources_for_viewer(
        [
            _sources_section("art_a"),
            _chart_section("sec_chart_1", "art_orphan"),
        ],
        [],
        alias_map=_aliases(),
        access_by_identity={"art_a": True, "art_orphan": True},
    )
    chart = next(s for s in projected if s["kind"] == "chart")
    assert "ordinal" not in chart["spec"]["source_meta"]


def test_chart_source_ordinal_absent_without_sources_section() -> None:
    """没有 sources section 时同样不下发（此前恒下发硬编 1）。"""
    projected, _ = project_sources_for_viewer(
        [_chart_section("sec_chart_1", "art_a")],
        [],
        alias_map=_aliases(),
        access_by_identity={"art_a": True},
    )
    chart = projected[0]
    assert "ordinal" not in chart["spec"]["source_meta"]


def test_chart_source_ordinal_skips_superseded_sources_section() -> None:
    """superseded 清单不参与编号（与 FE numberedSources 同规则）。"""
    projected, _ = project_sources_for_viewer(
        [
            _sources_section("art_old", superseded=True),
            _sources_section("art_new"),
            _chart_section("sec_chart_1", "art_new"),
        ],
        [],
        alias_map=_aliases(),
        access_by_identity={"art_old": True, "art_new": True},
    )
    chart = next(s for s in projected if s["kind"] == "chart")
    assert chart["spec"]["source_meta"]["ordinal"] == 1


def test_chart_source_alias_unchanged_by_display_ordinal() -> None:
    """slot 序号与展示序号解耦：展示号变了，alias 必须与旧行为完全一致
    （alias 随 sources 排序漂移会让缓存/授权记录全部失效）。"""
    aliases = _aliases()
    # Old chart source_meta slot always (section_id, "spec.source_meta", 1)
    expected_alias = aliases.alias_for("art_b", slot=("sec_chart_1", "spec.source_meta", 1))[0]
    projected, _ = project_sources_for_viewer(
        [
            _sources_section("art_a", "art_b"),
            _chart_section("sec_chart_1", "art_b"),
        ],
        [],
        alias_map=_aliases(),
        access_by_identity={"art_a": True, "art_b": True},
    )
    chart = next(s for s in projected if s["kind"] == "chart")
    assert chart["spec"]["source_meta"]["source_alias"] == expected_alias
    assert chart["spec"]["source_meta"]["ordinal"] == 2


def test_chart_source_ordinal_skips_sources_section_without_entries_array() -> None:
    """★首个 sources 段没有 entries 数组时要继续找下一段（与 FE numberedSources 同规则）。

    原实现的 break 在 isinstance 之外：停在那个空段上、映射表为空 → **所有图的
    [N] 全部消失**，而 FE 会用后一段正常渲出 [1..N]。注释当时还声称「规则与 FE
    一致」——不一致（review 逮到）。
    """
    broken_first = {"id": "sec_sources_broken", "kind": "sources", "entries": None}
    projected, _ = project_sources_for_viewer(
        [
            broken_first,
            _sources_section("art_a", "art_b"),
            _chart_section("sec_chart_1", "art_b"),
        ],
        [],
        alias_map=_aliases(),
        access_by_identity={"art_a": True, "art_b": True},
    )
    chart = next(s for s in projected if s["kind"] == "chart")
    assert chart["spec"]["source_meta"]["ordinal"] == 2


@pytest.mark.parametrize("state", ["linked_restricted", "linked_missing", "bogus"])
def test_kb_presentation_releases_detail_only_when_accessible(state: str) -> None:
    """知识库条目按白名单落笔：非可查看态即使上游误带了文件名 / 定位也一个不放（未知态按受限）。"""
    leaky = {"access_state": state, "safe_label": "机密.pdf", "kb_file_id": "kbf_x", "kb_chunk_id": "chunk_x", "kb_space_id": "ws_x"}
    sections = [{"id": "sec_src", "kind": "sources", "entries": [{"artifact_id": "chunk_x", "label": "机密.pdf"}]}]
    projected, _ = project_sources_for_viewer(
        sections,
        [],
        alias_map=_aliases(),
        access_by_identity={},
        kb_presentation_by_identity={"chunk_x": leaky},
    )
    entry = projected[0]["entries"][0]
    assert entry["access_state"] == ("linked_restricted" if state == "bogus" else state)
    assert set(entry) == {"id", "source_alias", "access_state", "generic_type", "ordinal"}
    assert "机密" not in repr(projected) and "kbf_x" not in repr(projected)

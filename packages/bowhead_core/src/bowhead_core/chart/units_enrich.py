"""Supplement unchanged source columns with compatible metadata.

Callers must establish source provenance before supplying a lookup. An available
Datarc connection and a matching header do not establish provenance for a Python,
uploaded, foreign-Tool, or earlier-turn Artifact. Values never determine a declared
percentage shape. Shared metadata projection lives in ``bowhead_core.units``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from bowhead_core import units
from bowhead_core.chart.numeric_display import source_numeric_format
from bowhead_core.chart.types import ChartColumn

logger = logging.getLogger(__name__)

MetadataLookup = Callable[[str], dict[str, str] | None]

# Enrich only these keys, and fill each only when empty.
_PATCHABLE = ("percent_scale", "unit", "format", "currency_code", "display")


def can_enrich_from_current_datarc_turn(artifact: Any, turn_id: str) -> bool:
    """Use the turn's selected source only for its own untransformed BI result.

    Artifacts do not persist a Datarc connection/project identity. Earlier-turn files
    therefore cannot safely borrow the current turn's metadata, even by exact name.
    The Artifact workspace_id identifies a Bowhead Space, not a Datarc project.
    """
    return bool(
        turn_id
        and getattr(artifact, "source", "") == "bi_fetch_rows"
        and getattr(artifact, "created_by_turn", None) == turn_id
        and not getattr(artifact, "derived_from", None)
    )


def lookup_from_source(client: Any, workspace_id: str) -> MetadataLookup | None:
    """把一个数据源 client 包成 lookup；该源不支持元数据或没有 workspace 则 None。

    数据源在这里是**可插拔**的：任何 client 只要提供
    `measure_metadata(workspace_id, name) -> {currency_code, value_format, unit_name}`
    （可选再提供 `dimension_metadata(...)`，形状相同，用于维度列换名）
    就能接进来，推断与投影共用 `bowhead_core.units` 那一份。

    ⚠ 只认**真的实现了这个方法的**源。先占位登记看着像已经支持了，实际每次都被
    hasattr 跳过，是「写了但从来跑不到」的典型。
    """
    if client is None or not workspace_id or not hasattr(client, "measure_metadata"):
        return None

    def _lookup(name: str) -> dict[str, str] | None:
        # Measures first, then dimensions. Charts contain both (`营业额 / 元` is a measure; `省` a dimension).
        # Handling only one leaves the other in Chinese on English turns, as observed on 116.
        hit = client.measure_metadata(workspace_id, name)
        if hit:
            return hit
        if hasattr(client, "dimension_metadata"):
            return client.dimension_metadata(workspace_id, name)
        return None

    return _lookup


def relabel_columns_for_locale(
    columns: list[ChartColumn],
    lookup: MetadataLookup,
    locale: str,
) -> int:
    """把列名里的**指标名那一截**换成该 locale 的展示标签，返回改动的列数。

    为什么不整个换掉列名：列名常带限定词，而限定词是**产出方加的、不可丢的信息**——
    `营业额 / 元` 的单位、`营业额(¥M)` 的量纲（那一轮 agent 真的除过 1e6）。
    整条换成 `GMV` 就把它们抹掉了，跟 2026-08-25 在 116 逮到的「给派生列套源单位」
    是同一类错。所以只替换开头那一截指标名，**限定词原样保留**：
    `营业额(¥M)` → `GMV(¥M)`。

    这也是为什么这里可以剥后缀、而 `enrich_columns_with_units` 不可以：
    那边是把**别处来的事实**（源指标的单位）安到派生列上，会与限定词矛盾；
    这里只是给同一个东西**换个称呼**，限定词不动，没有矛盾可言。
    """
    changed = 0
    for col in columns:
        header = (col.header or "").strip()
        if not header:
            continue
        try:
            label, rest = _relabel(header, lookup, locale)
        except Exception as e:  # noqa: BLE001 — Optional relabeling must never fail chart creation.
            logger.debug("units.relabel_failed col=%s err=%s", col.key, e)
            continue
        if not label:
            continue
        new_header = label + rest
        if new_header != col.header:
            # Keep the original source label reachable; display names do not establish numeric meaning.
            if not col.source_header:
                col.source_header = col.header
            col.header = new_header
            changed += 1
    return changed


def _relabel(header: str, lookup: MetadataLookup, locale: str) -> tuple[str, str]:
    """→ (该 locale 的展示标签, 要原样保留的限定词)；不该换时返回 ("", "")."""
    for name, rest in _name_and_qualifier(header):
        meta = lookup(name)
        if not meta:
            continue
        display = (meta.get("display_names") or {}).get(locale, "")
        display = str(display or "").strip()
        # Do not relabel an already canonical name, avoiding replacements such as `营业额` -> `营业额`.
        if not display or display == str(meta.get("primary_name") or "").strip():
            return "", ""
        return display, rest
    return "", ""


def _name_and_qualifier(header: str) -> list[tuple[str, str]]:
    """列名 → [(指标名候选, 其后要保留的限定词)]，整串优先。

    只认两种形状，都是生产里实测的：`营业额 / 元`（斜杠）与 `营业额(¥M)`（括号，
    含全角）。认不出就只试整串。
    """
    out: list[tuple[str, str]] = [(header, "")]
    for sep in (" / ", "/", "／"):
        if sep in header:
            head, _, tail = header.partition(sep)
            if head.strip():
                out.append((head.strip(), sep + tail))
            break
    m = _PAREN_TAIL_RE.search(header)
    if m and header[: m.start()].strip():
        out.append((header[: m.start()].strip(), header[m.start() :]))
    return out


_PAREN_TAIL_RE = re.compile(r"[（(][^（()）]*[)）]\s*$")


def enrich_columns_with_units(
    columns: list[ChartColumn],
    lookup: MetadataLookup,
    rows: list[dict[str, Any]] | None = None,
) -> int:
    """Fill absent compatible semantics and return the number of changed columns.

    Explicit declarations remain authoritative. Missing metadata remains missing;
    rows are retained in the call signature for compatibility, never as evidence
    to override a declared fraction/points representation.
    """
    changed = 0
    for col in columns:
        # Non-numeric columns have no units; enriching geo/date/categorical columns only pollutes rendering.
        if getattr(col, "type", None) != "numeric":
            continue
        header = col.header or col.key or ""
        # ★ Match only **exact column names**. Metadata describes source measures; names matching only after suffix removal fall into
        #   two cases: ① raw BI columns whose suffix already is the unit (`营业额 / 元`), making enrichment redundant;
        #   ② sandbox-derived columns (`营业额(¥M)` = original /1e6; `占比%` = *100),
        #      where **applying source units is wrong** by orders of magnitude.
        #   On 116 production (2026-08-25, nearly 60 charts and 89 numeric columns), suffix-stripped matching found only three columns:
        #   two redundant matches and one labeling `营业额(¥M)` as `元`, wrong by 10⁶. Zero benefit and one harm prompted exact matching again.
        #   Exact matching remains useful (`利润率` / `退款率` are actual measure names); deployments with complete metadata still benefit.
        meta = None
        try:
            meta = lookup(header.strip())
        except Exception as e:  # noqa: BLE001 — Optional enrichment must never fail chart creation.
            logger.debug("units.lookup_failed col=%s err=%s", col.key, e)
            continue
        if not meta:
            continue
        spec = units.resolve(
            header,
            currency_code=meta.get("currency_code", ""),
            value_format=meta.get("value_format", ""),
            unit_name=meta.get("unit_name", ""),
        )
        # Only source metadata can enrich semantics; names and numeric ranges are not evidence.
        patch = units.to_chart_column(spec) if spec is not None and spec.source == "metadata" else {}
        patch.update(source_numeric_format(meta.get("value_format")))
        if col.unit and patch.get("unit") and col.unit != patch["unit"] and col.currency_code != patch.get("currency_code"):
            # An explicit output unit cannot borrow another currency's identity or precision.
            continue
        declared_percent = col.percent_scale in ("fraction", "points") or col.format == "percent" or col.unit == "%"
        patch_percent = patch.get("percent_scale") in ("fraction", "points") or patch.get("format") == "percent"
        declared_other = col.format == "currency" or bool(col.unit and col.unit != "%")
        if (declared_percent and not patch_percent) or (declared_other and patch_percent):
            continue
        touched = False
        for key in _PATCHABLE:
            val = patch.get(key)
            if val and not getattr(col, key, None):
                setattr(col, key, val)
                touched = True
        if touched:
            changed += 1
    return changed


def mark_unknown_percent_shape(columns: list[ChartColumn]) -> int:
    """Record that no producer supplied a percentage shape.

    Kept for saved-spec compatibility and provenance. Readers require explicit
    fraction/points or a literal percent unit; they never guess from a name.
    """
    n = 0
    for c in columns:
        if getattr(c, "type", None) != "numeric":
            continue
        if c.percent_scale or c.format == "percent":
            # Source metadata / model / restored `15.37%` text already establishes shape; leave it unchanged.
            continue
        c.percent_shape_unknown = True
        n += 1
    return n

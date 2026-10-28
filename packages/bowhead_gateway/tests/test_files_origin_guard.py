"""file-management-v2 P0-4: source/kind→origin 唯一实现守卫。

历史事故（discovery §3.3 + 本 sprint review）：此映射曾在 routes/files.py、
routes/canvas.py、routes/sessions.py 三份手抄，互相漂移——canvas source-panel
与 Data tab 把 AI 生成背景图标成「上传」。现唯一实现在
bowhead_gateway/artifact_origin.py，本守卫钉四件事：

1. 生产者 source 全集 ⊆ SOURCE_TO_ORIGIN 键集（与 _SOURCE_TO_KIND 链接，
   两张 map 共用一份生产者口径）
2. 未知 source 兜底 = "other"（不得伪装成 upload）
3. 幻影枚举不复活（kb_extracted_record 无生产者）
4. routes/ 下不得再出现手抄的 source→origin 字典（第 4 份拷贝 = 架构错）
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.artifact_store import _SOURCE_TO_KIND, _kind_for_source
from bowhead_core.results_artifact_policy import SANDBOX_STDOUT_PRODUCER, is_results_internal_artifact
from bowhead_gateway.artifact_origin import SOURCE_TO_ORIGIN, VALID_ORIGINS, origin_for
from bowhead_gateway.routes.files import _ALLOWED_UPLOAD_SOURCES


def _literal_strings(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _literal_strings(node.body) | _literal_strings(node.orelse)
    return set()


def _local_literal_strings(function: ast.AST | None, name: ast.Name) -> set[str]:
    """Resolve a local variable only when every assignment to it in the enclosing function is literal."""
    if function is None:
        return set()
    assigned = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name.id for target in node.targets)
    ]
    other_bindings = [
        target
        for node in ast.walk(function)
        for target in (
            [node.target]
            if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr, ast.comprehension))
            else [item.optional_vars for item in node.items if item.optional_vars is not None]
            if isinstance(node, (ast.With, ast.AsyncWith))
            else []
        )
    ]
    rebound = any(isinstance(n, ast.Name) and n.id == name.id for target in other_bindings for n in ast.walk(target)) or any(
        isinstance(node, ast.arg) and node.arg == name.id for node in ast.walk(function)
    )
    resolved = [_literal_strings(value) for value in assigned]
    if rebound or not resolved or not all(resolved):
        return set()
    return set().union(*resolved)


def test_all_real_artifact_call_sources_mapped() -> None:
    """Read producer calls from source, instead of maintaining a hand-written set."""
    packages_dir = Path(__file__).resolve().parents[2]
    produced = set(_ALLOWED_UPLOAD_SOURCES)
    unresolved: list[str] = []
    for package in ("bowhead_core", "bowhead_gateway"):
        src_root = packages_dir / package / "src"
        for path in src_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            enclosing: dict[ast.AST, ast.AST] = {}
            for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
                for child in ast.walk(function):
                    enclosing[child] = function  # inner functions are walked later and win
            for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
                name = call.func.attr if isinstance(call.func, ast.Attribute) else ""
                if name not in {"put_bytes", "put_file"}:
                    continue
                source = next((kw.value for kw in call.keywords if kw.arg == "source"), None)
                if source is None:
                    produced.add("upload" if name == "put_bytes" else "canvas_export")
                    continue
                values = _literal_strings(source)
                if not values and isinstance(source, ast.Name):
                    # A local bound only to literal sources is as reviewable as an inline literal.
                    values = _local_literal_strings(enclosing.get(call), source)
                if values:
                    produced.update(values)
                elif not (
                    path.name == "artifact_store.py" or (path.name == "files.py" and isinstance(source, ast.Name) and source.id == "effective_source")
                ):
                    unresolved.append(f"{path.relative_to(packages_dir)}:{call.lineno}")

    assert not unresolved, f"动态 artifact source 未绑定权威 allowlist: {unresolved}"
    missing = produced - set(SOURCE_TO_ORIGIN)
    assert not missing, f"真实生产者 source 不在 SOURCE_TO_ORIGIN: {sorted(missing)}"


def test_kind_map_sources_also_have_origin() -> None:
    # Link the two authoritative maps: every source in _SOURCE_TO_KIND must also have an origin.
    # Either guard need catch a new producer only once to prevent omissions in both maps.
    missing = set(_SOURCE_TO_KIND) - set(SOURCE_TO_ORIGIN)
    assert not missing, f"_SOURCE_TO_KIND 有而 SOURCE_TO_ORIGIN 没有: {sorted(missing)}"


def test_map_values_are_valid_origins() -> None:
    bad = {s: o for s, o in SOURCE_TO_ORIGIN.items() if o not in VALID_ORIGINS}
    assert not bad, f"origin 枚举越界（i18n dataTab.origin.* 没这些键）: {bad}"


def test_unknown_source_falls_back_to_other() -> None:
    # P0-2: unknown sources must not masquerade as user uploads.
    assert origin_for(None, "brand_new_source") == "other"
    assert origin_for("tool_data", "") == "other"


def test_phantom_kb_extracted_record_stays_dead() -> None:
    # P0-3: no repository producer writes this source; map/frontend chip removed, must not revive.
    assert "kb_extracted_record" not in SOURCE_TO_ORIGIN


def test_origin_kind_precedence() -> None:
    # Exact producer/source precedes broad kind; deliverable means delivery, not necessarily a deck.
    assert origin_for("deliverable", "image_generate") == "compute"
    assert origin_for("web_clip", "web_fetch") == "web"
    assert origin_for("deliverable", "design_flatten") == "export"
    assert origin_for("deliverable", "x", producer_kind="design_flatten") == "export"
    assert origin_for("deliverable", "x", producer_kind="canvas_export_skill") == "export"
    assert origin_for("deliverable", "consultant_deck") == "deck"
    assert origin_for("deliverable", "unknown") == "other"


@dataclass(frozen=True)
class _ProducerContract:
    name: str
    source: str
    default_kind: str
    actual_kind: str
    producer_kind: str
    mime: str
    origin: str
    results_internal: bool = False


@pytest.mark.parametrize(
    "contract",
    [
        _ProducerContract("generated image evidence", "image_generate", "tool_data", "tool_data", "image_generate", "image/png", "compute"),
        _ProducerContract("generated image deliverable", "image_generate", "tool_data", "deliverable", "image_generate", "image/png", "compute"),
        _ProducerContract("flattened design", "design_flatten", "canvas_export", "deliverable", "design_flatten", "image/png", "export"),
        _ProducerContract(
            "canvas skill export", "canvas_export_skill", "deliverable", "deliverable", "canvas_export_skill", "application/pdf", "export"
        ),
        _ProducerContract(
            "consultant deck",
            "consultant_deck",
            "deliverable",
            "deliverable",
            "consultant_deck",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "deck",
        ),
        _ProducerContract("sandbox output", "python_sandbox", "sandbox_output", "sandbox_output", "python_sandbox", "text/csv", "compute"),
        # Two outputs of the same source have different user-facing roles: csv is requested, stdout is an
        # internal dependency of the result (the agent's own print output). origin stays compute; producer_kind
        # is absent from SOURCE_TO_ORIGIN, so `origin_for` deliberately falls back to source.
        _ProducerContract(
            "sandbox stdout", "python_sandbox", "sandbox_output", "sandbox_output", SANDBOX_STDOUT_PRODUCER, "text/plain", "compute", True
        ),
        _ProducerContract("BI rows", "bi_fetch_rows", "tool_data", "tool_data", "bi_fetch_rows", "text/csv", "query"),
        _ProducerContract("chart calculation", "chart_calculation", "tool_data", "tool_data", "chart_calculation", "text/csv", "compute"),
        _ProducerContract("web evidence", "web_fetch", "web_clip", "web_clip", "web_fetch", "text/markdown", "web"),
        _ProducerContract("input upload", "upload", "user_upload", "user_upload", "upload", "application/pdf", "upload"),
        _ProducerContract(
            "KB bundle grouping root", "kb_bundle_upload", "user_upload", "user_upload", "kb_bundle_upload", "application/zip", "upload", True
        ),
        _ProducerContract(
            "export manifest", "canvas_export", "canvas_export", "deliverable", "canvas_export_manifest_b1a_v1", "application/json", "export", True
        ),
    ],
    ids=lambda contract: contract.name,
)
def test_results_producer_contract(contract: _ProducerContract) -> None:
    """Audited producer intent: legacy kind, origin and Results admission."""
    assert _kind_for_source(contract.source) == contract.default_kind
    assert (
        origin_for(
            contract.actual_kind,
            contract.source,
            producer_kind=contract.producer_kind,
        )
        == contract.origin
    )
    artifact = SimpleNamespace(
        id="art_fixture",
        source=contract.source,
        kind=contract.actual_kind,
        producer_kind=contract.producer_kind,
    )
    assert is_results_internal_artifact(artifact) is contract.results_internal


def test_design_background_image_is_compute_not_upload() -> None:
    # Gap in discovery §3.3: design background images (image_generate + kind=tool_data)
    # previously fell back to legacy "upload" due to a missing map entry. After fix = compute.
    assert origin_for("tool_data", "image_generate") == "compute"


def test_no_handwritten_origin_map_in_routes() -> None:
    # A fourth handwritten copy is an architectural error. No file under routes/ may define a source→origin dict again
    # (fingerprinted by the necessarily paired key/value '"bi_fetch_rows": "query"').
    routes_dir = Path(__file__).resolve().parents[1] / "src" / "bowhead_gateway" / "routes"
    offenders = [p.name for p in routes_dir.glob("*.py") if '"bi_fetch_rows": "query"' in p.read_text(encoding="utf-8")]
    assert not offenders, f"routes/ 下出现手抄 source→origin map: {offenders}（用 bowhead_gateway.artifact_origin.origin_for）"

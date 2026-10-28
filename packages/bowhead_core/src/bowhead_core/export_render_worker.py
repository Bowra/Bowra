"""One-shot Canvas Export V2 format renderer with self-applied rlimits."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal, cast

_MAX_CONTROL_BYTES = 64 * 1024 * 1024


def _err(message: str) -> None:
    sys.stderr.write(f"export_render_worker: {message}\n")


def _cap(limit_id: int, value: int) -> bool:
    import resource

    _soft, hard = resource.getrlimit(limit_id)
    soft_only = value if hard == resource.RLIM_INFINITY else min(value, hard)
    for target in ((value, value), (soft_only, hard)):
        try:
            resource.setrlimit(limit_id, target)
            return True
        except (OSError, ValueError):
            continue
    return False


def _apply_self_rlimits(
    cpu_seconds: int,
    memory_mb: int,
    file_size_mb: int,
) -> None:
    """Bound the render child's CPU time and output file size.

    Memory is deliberately NOT bounded with RLIMIT_AS. RLIMIT_AS caps *virtual*
    address space, which LibreOffice reserves far more of than it ever resides
    (mmap'd libraries/fonts/heap arenas) — a per-format RLIMIT_AS sized to the
    design RSS budget SIGABRTs LibreOffice on a structurally complex workbook
    even though real RSS is a fraction of it (real 116 xlsx export failed with
    xlsx_libreoffice_recalc_failed at 768 MiB; needed ~1.28 GiB *virtual*).
    Real memory is instead bounded by the container's cgroup memory limit
    (deploy/compose.yaml `mem_limit`), the resident-memory lever headless
    LibreOffice services (gotenberg/unoserver) use — RLIMIT_RSS is ignored by
    modern kernels. `memory_mb` is retained as the observability budget only.
    """
    try:
        import resource
    except ImportError as exc:  # pragma: no cover - production is Linux
        raise RuntimeError("resource limits unavailable") from exc
    _ = memory_mb  # bounded by the container cgroup memory limit, not RLIMIT_AS
    limits = (
        (resource.RLIMIT_CPU, cpu_seconds),
        (resource.RLIMIT_FSIZE, file_size_mb * 1024 * 1024),
    )
    for limit_id, value in limits:
        if not _cap(limit_id, value):
            raise RuntimeError(f"setrlimit({limit_id}) failed")


def _read_control(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size <= 0 or size > _MAX_CONTROL_BYTES:
        raise ValueError("control size invalid")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("control must be an object")
    return cast(dict[str, Any], raw)


def _run(control: dict[str, Any], attempt_dir: Path) -> dict[str, Any]:
    from bowhead_core.export_validation import validate_export_output

    fmt = _required_string(control, "format")
    job_id = _required_string(control, "job_id")
    target = _required_string(control, "target")
    include_appendix = control.get("include_appendix")
    if not isinstance(include_appendix, bool):
        raise ValueError("include_appendix invalid")
    snapshot = _snapshot_from_dict(_required_dict(control, "snapshot"))
    export_ir = _export_ir_from_dict(_required_dict(control, "export_ir"))
    visuals = _visuals_from_list(_required_list(control, "materialized_visuals"))
    if (
        control.get("schema_version") != 1
        or snapshot.fmt != fmt
        or snapshot.profile != control.get("profile")
        or export_ir.snapshot_id != snapshot.id
    ):
        raise ValueError("control identity mismatch")

    if fmt == "pdf":
        (
            output,
            evidence,
            renderer_name,
            renderer_version,
            progress,
        ) = _run_pdf(
            attempt_dir=attempt_dir,
            export_ir=export_ir,
            print_theme=_frozen_print_theme(snapshot),
            visuals=visuals,
            pdftoppm_path=_optional_path(
                control,
                "pdftoppm_path",
            ),
        )
    elif fmt == "pptx":
        (
            output,
            evidence,
            renderer_name,
            renderer_version,
            progress,
        ) = _run_pptx(
            attempt_dir=attempt_dir,
            export_ir=export_ir,
            print_theme=_frozen_print_theme(snapshot),
            visuals=visuals,
            profile=_pptx_profile(snapshot.profile),
            include_appendix=include_appendix,
            soffice_path=_required_path(
                control,
                "soffice_path",
            ),
            pdftoppm_path=_required_path(
                control,
                "pdftoppm_path",
            ),
        )
    elif fmt == "xlsx":
        (
            output,
            evidence,
            renderer_name,
            renderer_version,
            progress,
        ) = _run_xlsx(
            attempt_dir=attempt_dir,
            export_ir=export_ir,
            visuals=visuals,
            soffice_path=_required_path(
                control,
                "soffice_path",
            ),
        )
    else:
        raise ValueError("unsupported format")

    _emit_validating(progress)
    manifest = validate_export_output(
        job_id=job_id,
        snapshot=snapshot,
        export_ir=export_ir,
        target=cast(Any, target),
        include_appendix=include_appendix,
        renderer_name=renderer_name,
        renderer_version=renderer_version,
        evidence=evidence,
    )
    return {
        "schema_version": 1,
        "format": fmt,
        "output": output,
        "manifest": manifest.to_dict(),
    }


def _run_pdf(
    *,
    attempt_dir: Path,
    export_ir: Any,
    print_theme: dict[str, Any],
    visuals: Any,
    pdftoppm_path: Path | None,
) -> tuple[
    dict[str, object],
    Any,
    str,
    str,
    dict[str, object],
]:
    from bowhead_core.export_pdf import (
        PDF_RENDERER_NAME,
        PDF_RENDERER_VERSION,
        extract_pdf_evidence,
        render_export_pdf,
    )

    rendered = render_export_pdf(
        export_ir=export_ir,
        materialized_visuals=visuals,
        attempt_dir=attempt_dir,
        print_theme=print_theme,
    )
    output_path = attempt_dir / rendered.relative_path
    evidence = extract_pdf_evidence(
        pdf_path=output_path,
        export_ir=export_ir,
        materialized_visuals=visuals,
        validation_dir=attempt_dir / "validation",
        pdftoppm_path=pdftoppm_path,
    )
    return (
        {
            "relative_path": rendered.relative_path,
            "content_sha256": rendered.content_sha256,
            "size_bytes": rendered.size_bytes,
        },
        evidence,
        PDF_RENDERER_NAME,
        PDF_RENDERER_VERSION,
        {
            "pages": rendered.page_count,
            "size_bytes": rendered.size_bytes,
        },
    )


def _run_pptx(
    *,
    attempt_dir: Path,
    export_ir: Any,
    print_theme: dict[str, Any],
    visuals: Any,
    profile: Literal["full_report", "executive_summary"],
    include_appendix: bool,
    soffice_path: Path,
    pdftoppm_path: Path,
) -> tuple[
    dict[str, object],
    Any,
    str,
    str,
    dict[str, object],
]:
    from bowhead_core.export_pptx import (
        PPTX_RENDERER_NAME,
        PPTX_RENDERER_VERSION,
        extract_pptx_evidence,
        render_export_pptx,
    )

    rendered = render_export_pptx(
        export_ir=export_ir,
        materialized_visuals=visuals,
        attempt_dir=attempt_dir,
        profile=profile,
        include_appendix=include_appendix,
        print_theme=print_theme,
    )
    output_path = attempt_dir / rendered.relative_path
    evidence = extract_pptx_evidence(
        pptx_path=output_path,
        export_ir=export_ir,
        materialized_visuals=visuals,
        profile=profile,
        include_appendix=include_appendix,
        validation_dir=attempt_dir / "validation",
        soffice_path=soffice_path,
        pdftoppm_path=pdftoppm_path,
    )
    return (
        {
            "relative_path": rendered.relative_path,
            "content_sha256": rendered.content_sha256,
            "size_bytes": rendered.size_bytes,
        },
        evidence,
        PPTX_RENDERER_NAME,
        PPTX_RENDERER_VERSION,
        {
            "slides": rendered.slide_count,
            "core_slides": rendered.core_slide_count,
            "size_bytes": rendered.size_bytes,
        },
    )


def _run_xlsx(
    *,
    attempt_dir: Path,
    export_ir: Any,
    visuals: Any,
    soffice_path: Path,
) -> tuple[
    dict[str, object],
    Any,
    str,
    str,
    dict[str, object],
]:
    from bowhead_core.export_xlsx import (
        XLSX_RENDERER_NAME,
        XLSX_RENDERER_VERSION,
        extract_xlsx_evidence,
        render_export_xlsx,
    )

    rendered = render_export_xlsx(
        export_ir=export_ir,
        materialized_visuals=visuals,
        attempt_dir=attempt_dir,
    )
    output_path = attempt_dir / rendered.relative_path
    evidence = extract_xlsx_evidence(
        xlsx_path=output_path,
        export_ir=export_ir,
        materialized_visuals=visuals,
        validation_dir=attempt_dir / "validation",
        soffice_path=soffice_path,
    )
    return (
        {
            "relative_path": rendered.relative_path,
            "content_sha256": rendered.content_sha256,
            "size_bytes": rendered.size_bytes,
        },
        evidence,
        XLSX_RENDERER_NAME,
        XLSX_RENDERER_VERSION,
        {
            "sheets": rendered.sheet_count,
            "data_sheets": rendered.data_sheet_count,
            "size_bytes": rendered.size_bytes,
        },
    )


def _frozen_print_theme(snapshot: Any) -> dict[str, Any]:
    """Return the print theme frozen into the job; the native renderers validate it."""

    request = snapshot.payload.get("request")
    print_theme = request.get("print_theme") if isinstance(request, dict) else None
    if not isinstance(print_theme, dict):
        raise ValueError("snapshot print theme missing")
    return cast(dict[str, Any], print_theme)


def _snapshot_from_dict(raw: dict[str, Any]) -> Any:
    from bowhead_core.export_snapshot import ExportSnapshot

    return ExportSnapshot(
        id=_required_string(raw, "id"),
        schema_version=_required_int(raw, "schema_version"),
        canvas_id=_required_string(raw, "canvas_id"),
        session_id=_required_string(raw, "session_id"),
        team_id=_optional_string(raw, "team_id"),
        workspace_id=_required_string(raw, "workspace_id"),
        canvas_owner=_required_string(raw, "canvas_owner"),
        requested_by=_required_string(raw, "requested_by"),
        fmt=_required_string(raw, "fmt"),
        profile=_required_string(raw, "profile"),
        title=_required_string(raw, "title"),
        locale=_required_string(raw, "locale"),
        timezone=_required_string(raw, "timezone"),
        projection_token=_required_string(
            raw,
            "projection_token",
        ),
        surface_contract_version=_required_int(
            raw,
            "surface_contract_version",
        ),
        surface_token=_required_string(raw, "surface_token"),
        parity_contract=_required_string(
            raw,
            "parity_contract",
        ),
        payload=_required_dict(raw, "payload"),
        payload_sha256=_required_string(raw, "payload_sha256"),
        size_bytes=_required_int(raw, "size_bytes"),
        section_count=_required_int(raw, "section_count"),
        created_at=_required_string(raw, "created_at"),
        expires_at=_required_string(raw, "expires_at"),
    )


def _export_ir_from_dict(raw: dict[str, Any]) -> Any:
    from bowhead_core.export_ir import (
        ExportIR,
        ExportIRBlock,
        ExportIRDocument,
        ExportIRSource,
    )

    def source(value: dict[str, Any]) -> Any:
        return ExportIRSource(
            section_id=_required_string(value, "section_id"),
            section_rev=_required_int(value, "section_rev"),
            section_kind=_required_string(
                value,
                "section_kind",
            ),
            section_path=tuple(_int_list(value, "section_path")),
            surface_placement=_required_string(
                value,
                "surface_placement",
            ),
        )

    def block(value: dict[str, Any]) -> Any:
        return ExportIRBlock(
            block_key=_required_string(value, "block_key"),
            block_kind=cast(
                Any,
                _required_string(value, "block_kind"),
            ),
            order=_required_int(value, "order"),
            source=source(_required_dict(value, "source")),
            source_refs=tuple(_string_list(value, "source_refs")),
            content=_required_dict(value, "content"),
            semantic_tokens=tuple(_string_list(value, "semantic_tokens")),
            semantic_content_sha256=_required_string(
                value,
                "semantic_content_sha256",
            ),
            typed_data_sha256=_optional_string(
                value,
                "typed_data_sha256",
            ),
            visual_input_sha256=_optional_string(
                value,
                "visual_input_sha256",
            ),
            warnings=tuple(_string_list(value, "warnings")),
            children=tuple(block(child) for child in _dict_list(value, "children")),
        )

    document = _required_dict(raw, "document")
    return ExportIR(
        schema_version=_required_int(raw, "schema_version"),
        snapshot_id=_required_string(raw, "snapshot_id"),
        input_sha256=_required_string(raw, "input_sha256"),
        surface_token=_required_string(raw, "surface_token"),
        document=ExportIRDocument(
            title=_required_string(document, "title"),
            locale=_required_string(document, "locale"),
            timezone=_required_string(document, "timezone"),
            profile=_required_string(document, "profile"),
        ),
        blocks=tuple(block(value) for value in _dict_list(raw, "blocks")),
        visual_assets=tuple(_dict_list(raw, "visual_assets")),
        sources=tuple(_dict_list(raw, "sources")),
        normalization_warnings=tuple(_string_list(raw, "normalization_warnings")),
    )


def _visuals_from_list(raw: list[Any]) -> Any:
    from bowhead_core.export_materializer import MaterializedVisual

    visuals: list[Any] = []
    for value in raw:
        if not isinstance(value, dict):
            raise ValueError("materialized visual invalid")
        visual = cast(dict[str, Any], value)
        raw_hashes = _required_list(
            visual,
            "source_artifact_hashes",
        )
        hashes: list[tuple[str, str]] = []
        for item in raw_hashes:
            if not isinstance(item, list) or len(item) != 2 or not all(isinstance(part, str) for part in item):
                raise ValueError("visual hashes invalid")
            hashes.append((item[0], item[1]))
        visuals.append(
            MaterializedVisual(
                block_key=_required_string(
                    visual,
                    "block_key",
                ),
                section_id=_required_string(
                    visual,
                    "section_id",
                ),
                section_rev=_required_int(
                    visual,
                    "section_rev",
                ),
                section_kind=_required_string(
                    visual,
                    "section_kind",
                ),
                visual_input_sha256=_required_string(
                    visual,
                    "visual_input_sha256",
                ),
                source_artifact_hashes=tuple(hashes),
                relative_path=_required_string(
                    visual,
                    "relative_path",
                ),
                mime=_required_string(visual, "mime"),
                content_sha256=_required_string(
                    visual,
                    "content_sha256",
                ),
                pixel_sha256=_required_string(
                    visual,
                    "pixel_sha256",
                ),
                width=_required_int(visual, "width"),
                height=_required_int(visual, "height"),
                renderer_name=_required_string(
                    visual,
                    "renderer_name",
                ),
                renderer_version=_required_string(
                    visual,
                    "renderer_version",
                ),
                warnings=tuple(_string_list(visual, "warnings")),
            )
        )
    return tuple(visuals)


def _emit_validating(progress: dict[str, object]) -> None:
    sys.stdout.write(
        json.dumps(
            {
                "phase": "validating",
                "progress": progress,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()


def _write_result(
    path: Path,
    result: dict[str, Any],
) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _required_dict(
    value: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    raw = value.get(key)
    if not isinstance(raw, dict):
        raise ValueError(f"{key} invalid")
    return cast(dict[str, Any], raw)


def _required_list(
    value: dict[str, Any],
    key: str,
) -> list[Any]:
    raw = value.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"{key} invalid")
    return raw


def _dict_list(
    value: dict[str, Any],
    key: str,
) -> list[dict[str, Any]]:
    raw = _required_list(value, key)
    if not all(isinstance(item, dict) for item in raw):
        raise ValueError(f"{key} invalid")
    return cast(list[dict[str, Any]], raw)


def _string_list(
    value: dict[str, Any],
    key: str,
) -> list[str]:
    raw = _required_list(value, key)
    if not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{key} invalid")
    return cast(list[str], raw)


def _int_list(
    value: dict[str, Any],
    key: str,
) -> list[int]:
    raw = _required_list(value, key)
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in raw):
        raise ValueError(f"{key} invalid")
    return cast(list[int], raw)


def _required_string(
    value: dict[str, Any],
    key: str,
) -> str:
    raw = value.get(key)
    if not isinstance(raw, str):
        raise ValueError(f"{key} invalid")
    return raw


def _optional_string(
    value: dict[str, Any],
    key: str,
) -> str | None:
    raw = value.get(key)
    if raw is not None and not isinstance(raw, str):
        raise ValueError(f"{key} invalid")
    return raw


def _required_int(
    value: dict[str, Any],
    key: str,
) -> int:
    raw = value.get(key)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(f"{key} invalid")
    return raw


def _required_path(
    value: dict[str, Any],
    key: str,
) -> Path:
    raw = _required_string(value, key)
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{key} invalid")
    return path


def _optional_path(
    value: dict[str, Any],
    key: str,
) -> Path | None:
    raw = _optional_string(value, key)
    if raw is None:
        return None
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{key} invalid")
    return path


def _pptx_profile(
    value: str,
) -> Literal["full_report", "executive_summary"]:
    if value not in {"full_report", "executive_summary"}:
        raise ValueError("profile invalid")
    return cast(
        Literal["full_report", "executive_summary"],
        value,
    )


def main() -> int:
    if len(sys.argv) != 6:
        _err("usage: export_render_worker <control> <result> <cpu_s> <memory_mb> <file_size_mb>")
        return 2
    control_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    try:
        _apply_self_rlimits(
            int(sys.argv[3]),
            int(sys.argv[4]),
            int(sys.argv[5]),
        )
        attempt_dir = control_path.parent.resolve()
        if result_path.parent.resolve() != attempt_dir or control_path.is_symlink() or result_path.is_symlink():
            raise ValueError("worker path invalid")
        control = _read_control(control_path)
        result = _run(control, attempt_dir)
        _write_result(result_path, result)
    except MemoryError:
        # Real memory is bounded by the container cgroup (an over-limit child is
        # SIGKILLed, surfaced as a non-zero return code); a Python-level
        # MemoryError here is a best-effort in-process guard.
        _err("MemoryError")
        return 3
    except Exception as exc:
        _err(f"{type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

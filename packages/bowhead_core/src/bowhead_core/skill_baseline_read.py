"""Read an authorized retained baseline package without opening today's files.

This pure projection preserves ordinary SOP text and reference/argument reading.
It never grants tools, executes scripts, or substitutes selected method parameters.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import yaml
from bowhead_skill.bundle_files import (
    MAX_TEXT_BYTES,
    BundleFileError,
    package_file_listing,
    package_miss,
    package_read_result,
    resolve_package_path,
)
from bowhead_skill.index import substitute_args
from bowhead_skill.loader import split_skill_markdown
from bowhead_skill.script_langs import entry_extensions, script_execution_mode

from bowhead_core.chat_references import _bounded_json
from bowhead_core.skill_package_material import PreparedSkillPackage


def snapshot_skill_read_arguments(arguments: dict) -> dict:
    """Detach bounded tool arguments before any authorization await.

    `null` or empty optionals count as omitted, as the ordinary handler treats
    them; every exact layer compares this snapshot. Unknown keys stay refused.
    """
    try:
        if type(arguments) is not dict or set(arguments) - {"name", "file", "ref", "args"}:
            raise ValueError()
        arguments = {key: value for key, value in arguments.items() if key == "name" or value not in (None, "")}
        if (
            type(arguments.get("name")) is not str
            or not arguments["name"].strip()
            or any(key in arguments and type(arguments[key]) is not str for key in ("file", "ref"))
        ):
            raise ValueError()
        raw = arguments.get("args", "")
        if type(raw) is not str and (type(raw) is not list or any(type(item) is not str for item in raw)):
            raise ValueError()
        _bounded_json(arguments, max_depth=3, max_bytes=64 * 1024)
        return {**arguments, "name": arguments["name"].strip(), "args": list(raw) if type(raw) is list else raw}
    except Exception:
        raise ValueError("reference_skill_arguments_unavailable") from None


def read_package_path(name: str, files: dict[str, bytes], requested: str, *, prefer_references: bool) -> dict:
    """Read one exact package file by the path the model passed.

    Same resolver and result shapes as the ordinary handler (skill-bundle-read
    tech-design §4). A miss is the model's to correct, so it returns the package
    files instead of raising.
    """
    listing = sorted((path, len(data)) for path, data in files.items())
    try:
        path = resolve_package_path(requested, files, prefer_references=prefer_references)
    except BundleFileError:
        return package_miss(name, requested, listing, reason="skill_path_invalid")
    if path is None:
        return package_miss(name, requested, listing)
    return package_read_result(name, path, files[path], ref_requested=prefer_references)


def read_baseline_package(package: PreparedSkillPackage, arguments: dict, *, invoke_allowed: bool = False) -> dict:
    """Caller separately proves exact receiver/read/current-source authority."""
    try:
        arguments = snapshot_skill_read_arguments(arguments)
        if arguments["name"] != package.name:
            raise ValueError()
        files = package.files()
        result = {
            "name": package.name,
            "resource": package.resource.model_dump(mode="json"),
            "read_contract": "skill-baseline-read.v1",
            "content_hash": package.content_hash,
            "materialization_hash": package.resource.version,
        }
        file, ref = arguments.get("file", "").strip(), arguments.get("ref", "").strip()
        if file or ref:
            read = read_package_path(package.name, files, file or ref, prefer_references=not file)
            # A miss stands alone; a resolved file (even one too large to show) keeps the package identity.
            return read if "requested" in read else {**result, **read}
        header, body = split_skill_markdown(files["SKILL.md"].decode("utf-8"))
        metadata = yaml.safe_load(header)
        allowed = metadata.get("allowed-tools") or metadata.get("allowed_tools") or []
        allowed = (
            [item.strip() for item in allowed.split(",") if item.strip()]
            if isinstance(allowed, str)
            else ([str(item) for item in allowed] if isinstance(allowed, list) else [])
        )
        scripts = [
            path.removeprefix("scripts/")
            for path in files
            if path.startswith("scripts/") and PurePosixPath(path).name != "__init__.py" and PurePosixPath(path).suffix in entry_extensions()
        ]
        result.update(
            description=str(metadata.get("description") or ""),
            version=str(metadata["version"]) if metadata.get("version") is not None else None,
            body=substitute_args(body, arguments["args"], max_output_bytes=MAX_TEXT_BYTES),
            allowed_tools=allowed,
            references=[path.removeprefix("references/") for path in files if path.startswith("references/")],
            scripts=scripts,
            **package_file_listing(sorted((path, len(data)) for path, data in files.items())),
            script_execution=(
                script_execution_mode(
                    name=package.name,
                    has_scripts=bool(scripts),
                    builtin=package.resource.source_kind == "builtin",
                    knowledge=metadata.get("kind") == "knowledge",
                )
                if invoke_allowed
                else "disabled"
                if scripts and metadata.get("kind") != "knowledge"
                else "none"
            ),
        )
        if metadata.get("pipes_output_to"):
            result["pipes_output_to"] = str(metadata["pipes_output_to"])
            result["next_step_hint"] = (
                f'完成本 SOP 的分析后，下一步必须调 skill_read(name="{result["pipes_output_to"]}") 看输出契约，再调对应工具收尾。'
            )
        return result
    except Exception:
        raise ValueError("reference_skill_baseline_read_unavailable") from None

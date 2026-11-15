"""Advisory HTML structure checks; never execute or rewrite authored content."""

from __future__ import annotations

import hashlib
import logging
import re
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import unquote

import anyio

from bowhead_core.html_preview_policy import html_preview_limit_bytes

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact, ArtifactStore

_MAX_FINDINGS = 20
_MAX_DETAIL = 120
_MAX_ELEMENTS = 100_000
# The interactive preview's script-text budget; larger scripts skip only the lookup check.
_MAX_SCRIPT_CHARS = 8 * 1024 * 1024
_LOG = logging.getLogger(__name__)
HTML_STATIC_CHECK_GUIDANCE = (
    "Retained HTML outputs may include html_static_check. Repair reported structure or offline dependencies "
    "before claiming the affected controls work. script_id_reference_missing lists literal element lookups in "
    "inline scripts with no matching element; when reusing a reference file's scripts, keep the elements they "
    "look up or adapt the lookups. not_checked and runtime_checked=false do not certify "
    "browser interactions or data correctness. An unavailable advisory check never undoes a saved file."
)
_SCRIPT_TYPES = frozenset({"", "text/javascript", "application/javascript", "module", "text/ecmascript", "application/ecmascript"})
_NAME = r"[A-Za-z_$][\w$]*"
# A quoted literal ID; its quote is group 1 wherever it is used.
_LITERAL = r"(['\"`])([^'\"`\\$\s<>]{1,120})\1"
# Local one-argument helpers whose body is document.getElementById(arg), e.g. `const $ = (id) => document.getElementById(id)`.
_ARROW_HELPER = re.compile(
    rf"(?:const|let|var)\s+({_NAME})\s*=\s*(?:\(\s*({_NAME})\s*\)|({_NAME}))\s*=>\s*(?:\{{\s*return\s+)?document\s*\.\s*getElementById\(\s*(?:\2|\3)\s*\)"
)
_FUNCTION_HELPER = re.compile(rf"function\s+({_NAME})\s*\(\s*({_NAME})\s*\)\s*\{{\s*return\s+document\s*\.\s*getElementById\(\s*\2\s*\)")
_GET_BY_ID = re.compile(r"getElementById\(\s*" + _LITERAL + r"\s*\)")
_QUERY_ID = re.compile(r"querySelector(?:All)?\(\s*(['\"`])#([A-Za-z_][\w:-]{0,119})\1\s*\)")
# IDs a script creates: markup strings, property assignment and setAttribute.
_SCRIPT_DEFINED_IDS = (
    re.compile(r"\bid\s*=\s*\\?[\"']([^\"'\\\s<>]{1,120})\\?[\"']"),
    re.compile(r"\.id\s*=\s*(['\"`])([^'\"`\\$\s<>]{1,120})\1"),
    re.compile(r"setAttribute\(\s*['\"]id['\"]\s*,\s*(['\"`])([^'\"`\\$\s<>]{1,120})\1"),
)


class _StructureLimitExceeded(ValueError):
    pass


class _StructureCheck(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: dict[str, str] = {}
        self.anchor_names: set[str] = set()
        self.links: list[tuple[str, int, int]] = []
        self.findings: list[dict[str, object]] = []
        self.total = 0
        self.template_depth = 0
        self.elements = 0
        self.template_ids: set[str] = set()
        # Executable inline script text with the document position where each chunk starts.
        self.scripts: list[tuple[str, int, int]] = []
        self.script_chars = 0
        self.in_script = False

    def finding(self, code: str, value: str, line: int, column: int, **details: str) -> None:
        self.total += 1
        if len(self.findings) < _MAX_FINDINGS:
            self.findings.append(
                {
                    "code": code,
                    "value": value[:_MAX_DETAIL],
                    "line": line,
                    "column": column,
                    **{key: detail[:_MAX_DETAIL] for key, detail in details.items()},
                }
            )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements += 1
        if self.elements > _MAX_ELEMENTS:
            raise _StructureLimitExceeded
        values = dict(attrs)
        if self.template_depth:
            if tag == "template":
                self.template_depth += 1
            # Dormant until cloned: never a live target, but scripts may clone it.
            if values.get("id"):
                self.template_ids.add(values["id"] or "")
            return
        line, offset = self.getpos()
        column = offset + 1
        identity = values.get("id")
        if identity:
            if identity in self.ids:
                self.finding("duplicate_id", identity, line, column, tag=tag, first_tag=self.ids[identity])
            else:
                self.ids[identity] = tag
        if tag == "a" and values.get("name"):
            self.anchor_names.add(values["name"] or "")
        if tag in {"a", "area"}:
            href = (values.get("href") or "").strip()
            if href.startswith("about:srcdoc#"):
                href = href.removeprefix("about:srcdoc")
            if href.startswith("#"):
                target = unquote(href[1:]).partition(":~:")[0]
                if target and target.lower() != "top":
                    self.links.append((target, line, column))
        resource_attributes = {
            "script": ("src",),
            "link": ("href",),
            "img": ("src", "srcset"),
            "audio": ("src",),
            "video": ("src", "poster"),
            "source": ("src", "srcset"),
        }
        for attribute in resource_attributes.get(tag, ()):
            resource = (values.get(attribute) or "").strip()
            if resource and (tag in {"script", "link"} or not resource.lower().startswith("data:")):
                self.finding("external_dependency", "non_inline_resource", line, column, tag=tag, attribute=attribute)
        if tag == "template":
            self.template_depth += 1
        if tag == "script" and not values.get("src"):
            self.in_script = (values.get("type") or "").strip().lower() in _SCRIPT_TYPES

    def handle_endtag(self, tag: str) -> None:
        if tag == "template" and self.template_depth:
            self.template_depth -= 1
        if tag == "script":
            self.in_script = False

    def handle_data(self, data: str) -> None:
        if self.in_script and not self.template_depth:
            line, offset = self.getpos()
            self.scripts.append((data, line, offset))
            self.script_chars += len(data)

    def check_script_lookups(self) -> bool:
        """Report literal element lookups with no element; False when over the script budget."""
        if self.script_chars > _MAX_SCRIPT_CHARS:
            return False
        text = "\n".join(chunk for chunk, _, _ in self.scripts)
        defined = set(self.ids) | self.template_ids
        for pattern in _SCRIPT_DEFINED_IDS:
            defined.update(match.group(match.lastindex or 1) for match in pattern.finditer(text))
        helpers = {match.group(1) for match in _ARROW_HELPER.finditer(text)} | {match.group(1) for match in _FUNCTION_HELPER.finditer(text)}
        lookups = [_GET_BY_ID, _QUERY_ID]
        if helpers:
            names = "|".join(re.escape(name) for name in sorted(helpers))
            lookups.append(re.compile(r"(?<![\w$.])(?:" + names + r")\(\s*" + _LITERAL + r"\s*\)"))
        reported: set[str] = set()
        for chunk, line, offset in self.scripts:
            hits = sorted((match.start(), match.group(2)) for pattern in lookups for match in pattern.finditer(chunk))
            for position, identity in hits:
                if identity in defined or identity in reported:
                    continue
                reported.add(identity)
                newlines = chunk.count("\n", 0, position)
                column = position - chunk.rfind("\n", 0, position) if newlines else offset + position + 1
                self.finding("script_id_reference_missing", identity, line + newlines, column)
        return True


def inspect_html(content: bytes) -> dict[str, object]:
    """Report definite static findings, not functional or data correctness."""
    try:
        source = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"status": "not_checked", "reason": "unsupported_encoding", "runtime_checked": False}
    parser = _StructureCheck()
    try:
        parser.feed(source)
        parser.close()
    except _StructureLimitExceeded:
        return {"status": "not_checked", "reason": "structure_budget", "runtime_checked": False}
    for target, line, column in parser.links:
        if target not in parser.ids and target not in parser.anchor_names:
            parser.finding("missing_fragment_target", target, line, column)
    checks = ["duplicate_ids", "fragment_targets", "resource_attributes"]
    if parser.check_script_lookups():
        checks.append("script_id_references")
    return {
        "status": "issues_found" if parser.total else "no_static_issues_found",
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "checks": checks,
        "runtime_checked": False,
        "findings": parser.findings,
        "total_findings": parser.total,
        "truncated": parser.total > len(parser.findings),
        "guidance": HTML_STATIC_CHECK_GUIDANCE,
    }


async def inspect_retained_html(store: ArtifactStore, artifact: Artifact) -> dict[str, object] | None:
    """Read only after original artifact authorization; retain delivery on inspection failure."""
    if (artifact.mime or "").split(";", 1)[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
        return None
    limit = html_preview_limit_bytes()
    if artifact.size > limit:
        return {"status": "not_checked", "reason": "preview_byte_limit", "runtime_checked": False}
    try:
        content = await store.backend.read_bounded(artifact.path, limit=limit)
        if len(content) != artifact.size:
            return {"status": "not_checked", "reason": "content_changed", "runtime_checked": False}
        return await anyio.to_thread.run_sync(inspect_html, content)
    except Exception as exc:
        # An advisory read cannot revoke a completed file write or invite regeneration.
        _LOG.warning("html_static_check.unavailable artifact_id=%s error_type=%s", artifact.id, type(exc).__name__)
        return {"status": "not_checked", "reason": "inspection_unavailable", "runtime_checked": False}

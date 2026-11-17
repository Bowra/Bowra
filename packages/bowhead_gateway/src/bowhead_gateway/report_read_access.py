"""Actor-bound report reads using existing Canvas and source authorities."""

from __future__ import annotations

import logging
from copy import copy
from dataclasses import dataclass, replace
from typing import Any

from bowhead_core.ordinary_lead_continuation import content_hash
from bowhead_core.report_read_material import ReportReadObservation, report_read_validation_pass, validation_pass_cache
from bowhead_core.source_access_projection import canonical_source_identity, collect_source_identities, names_no_source

logger = logging.getLogger(__name__)


@dataclass
class _SnapshotStore:
    canvas: Any
    sections: list[Any]
    original: Any

    async def get_canvas_by_session(self, session_id):
        return self.canvas if self.canvas.session_id == session_id else None

    async def list_sections(self, canvas_id, *, include_hidden=False):
        return [s for s in self.sections if s.canvas_id == canvas_id and (include_hidden or not s.hidden_for_chat_mode)]

    async def section_ids_touched_by_turn(self, turn_id):
        return await self.original.section_ids_touched_by_turn(turn_id)


def _failure(code: str, message: str):
    return {"ok": False, "error": code, "message": message}, None


@dataclass(frozen=True)
class ReportReadAccess:
    request: Any
    session_id: str
    workspace_id: str
    team_id: str
    user_id: str

    async def project_chart(self, section: Any) -> dict[str, Any]:
        """Use the admitted actor and rechecked scope, including after HTTP/resume boundaries.

        Raises ``ChartProjectionUnavailable`` with the failing check.
        """
        from bowhead_gateway.chart_authoring_projection import project_chart_for_authoring

        return await project_chart_for_authoring(self.request, self.session_id, section, access=self)

    async def chart_palette(self) -> list[dict[str, Any]]:
        """Offer the same validated palette slots used by screen and export."""
        from bowhead_core.chart.series_colors import palette_color_family

        from bowhead_gateway.routes.theme import resolve_chart_style_v2_for_current_theme

        await self._canvas()
        state = self.request.app.state
        user = await state.user_store.get_user(self.user_id)
        if user is None:
            raise ValueError("Chart palette actor unavailable")
        user = replace(user, team_id=self.team_id, active_team_id=self.team_id)
        effective, _ = await resolve_chart_style_v2_for_current_theme(
            self.request,
            service=state.chart_style_read_service,
            team_store=state.team_settings_store,
            user_store=state.user_settings_store,
            user=user,
        )
        modes = effective["modeColors"]
        light = modes["light"]["categoricalSeriesPalette"]
        dark = modes["dark"]["categoricalSeriesPalette"]
        # The base palette is shared by all modes; generated overflow slots are not authoring choices.
        count = len(effective["style"]["resolvedPresetSnapshot"]["colors"]["light"]["categorical"])
        return [{"index": i, "family": palette_color_family(light[i]), "light": light[i], "dark": dark[i]} for i in range(count)]

    async def _canvas(self):
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope

        scope = ReferenceRequestScope(team_id=self.team_id, user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id)
        async with open_shared_ctx(self.request.app.state.session_store.db_path) as db:
            await require_reference_request_scope(db, scope)
        store = self.request.app.state.canvas_store
        canvas = await store.get_canvas_by_session(self.session_id)
        if canvas is not None and (canvas.workspace_id != self.workspace_id or canvas.user_id != self.user_id):
            raise ValueError("Report unavailable")
        return canvas

    async def _sources(self, canvas, sections, refs):
        from bowhead_gateway.kb_citation_access import kb_file_access_for_user
        from bowhead_gateway.report_artifact_access import authorized_report_artifact_ids
        from bowhead_gateway.source_access_projection import ViewerSafeCanvasProjector, source_ref_artifact_ids

        identities = collect_source_identities(sections, refs)
        artifacts = list(dict.fromkeys([identity for identity in identities if identity.startswith("art_")] + list(source_ref_artifact_ids(refs))))
        allowed = await authorized_report_artifact_ids(self.request, canvas, artifacts, viewer_id=self.user_id)
        return await ViewerSafeCanvasProjector.build(
            root_secret=getattr(self.request.app.state, "artifact_fetch_secret", None),
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            sections=sections,
            source_refs=refs,
            report_artifact_closure=list(allowed),
            artifact_store=self.request.app.state.artifact_store,
            kb_file_access=kb_file_access_for_user(
                self.request.app, await self.request.app.state.user_store.get_user(self.user_id), expected_team_id=self.team_id
            ),
        )

    async def _required_sources(self, section):
        canonical = section.to_report_section()
        if section.kind == "chart":
            from bowhead_gateway.chart_authoring_projection import chart_source_requirements

            identities, _, _ = await chart_source_requirements(self.request.app.state.artifact_store, canonical)
            return identities
        return collect_source_identities([canonical])

    @staticmethod
    def _recorded(refs) -> frozenset[str]:
        return frozenset(identity for identity in (canonical_source_identity(ref) for ref in refs) if isinstance(identity, str))

    @staticmethod
    def _authorizable(identities, recorded: frozenset[str]) -> list[str]:
        """Leave out bare numbers that no recorded source carries.

        Such a value names nothing to authorize. Kept as a requirement it hid the
        whole section from every read: on 116 (2026-11-15) a helper wrote
        ``source_refs`` ["100", "101", "105"] and later helpers reported that
        chapter missing. Every other identity still has to be authorized.
        """
        return [identity for identity in identities if identity in recorded or not names_no_source(identity)]

    async def recorded_source_identities(self) -> frozenset[str]:
        """Identities of the sources this conversation's Tool results recorded."""
        from bowhead_gateway.routes.canvas import _build_source_refs_for_session

        return self._recorded(await _build_source_refs_for_session(self.request, self.session_id, strict=True))

    async def read(self, arguments, ctx):
        from bowhead_core.agents.report_tools import _read_report_snapshot
        from bowhead_core.context_segments import MAX_CONTEXT_BYTES

        from bowhead_gateway.routes.canvas import _build_source_refs_for_session

        ids = arguments.get("section_ids", [])
        if (
            not isinstance(ids, list)
            or any(not isinstance(identity, str) or not identity for identity in ids)
            or type(arguments.get("include_full", False)) is not bool
        ):
            return _failure("invalid_report_selection", "Use section_ids as an array of existing IDs and include_full as a boolean.")
        if any(getattr(ctx, name, None) != getattr(self, name) for name in ("session_id", "workspace_id", "team_id", "user_id")):
            return _failure("not_found_or_unavailable", "The requested report is unavailable in this conversation.")
        try:
            canvas = await self._canvas()
            if canvas is None:
                if ids:
                    return _failure("not_found_or_unavailable", "The selected sections are unavailable in this conversation.")
                result = {"ok": True, "outline": [], "sections": [], "figures": [], "coverage": {"view": "outline", "complete": True}}
                return result, self._observation(None, [], [], [], result)
            store = self.request.app.state.canvas_store
            snapshot = await store.read_canvas_projection_state(canvas.id)
            if snapshot is None:
                raise ValueError("Report unavailable")
            canvas, all_sections = snapshot
            refs = await _build_source_refs_for_session(self.request, self.session_id, strict=True)
            recorded = self._recorded(refs)
            visible = [s for s in all_sections if not s.quarantined]
            required = {}
            for section in visible:
                try:
                    required[section.id] = self._authorizable(await self._required_sources(section), recorded)
                except ValueError:
                    continue
            canonical = [{"kind": "narrative", "source_refs": ids} for ids in required.values()]
            identities = collect_source_identities(canonical)
            relevant_refs = [ref for ref in refs if canonical_source_identity(ref) in identities]
            projector = await self._sources(canvas, canonical, relevant_refs)
            sections = [
                section
                for section in visible
                if section.id in required and all(projector.access_by_identity.get(identity, False) for identity in required[section.id])
            ]
            selected = {s.id for s in sections if not s.hidden_for_chat_mode}
            if any(identity not in selected for identity in ids):
                return _failure(
                    "not_found_or_unavailable", "Some selected sections are unavailable. Read the current outline and select accessible IDs."
                )
            read_ctx = copy(ctx)
            read_ctx.canvas_store = _SnapshotStore(canvas, sections, store)
            result = await _read_report_snapshot(arguments, read_ctx)
            returned = {row["section_id"] for row in result.get("outline", [])} | {row["id"] for row in result.get("figures", [])}
            observed = [s for s in sections if s.id in returned]
            source_sections = [{"kind": "narrative", "source_refs": required[s.id]} for s in observed]
            source_ids = collect_source_identities(source_sections)
            source_refs = [ref for ref in relevant_refs if canonical_source_identity(ref) in source_ids]
            result["coverage"] = {
                "view": "selected_content" if ids or arguments.get("include_full") else "outline",
                "complete": len(sections) == len(all_sections),
                "omissions": [] if len(sections) == len(all_sections) else ["unavailable_content"],
                "non_text_section_ids": [row["section_id"] for row in result.get("sections", []) if row.get("content") is None],
                "snapshot": "current",
            }
            import json

            from bowhead_core.token_estimator import count_tokens_of_obj

            token_budget = getattr(ctx, "material_read_token_budget", None)
            if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES or (
                isinstance(token_budget, int) and count_tokens_of_obj(result) > token_budget
            ):
                return _failure(
                    "report_read_too_large",
                    "Read the outline, then select fewer section IDs. An oversized individual section cannot be read in this context.",
                )
            after = await store.read_canvas_projection_state(canvas.id)
            if after is None or content_hash(
                [s.to_report_section() | {"hidden": s.hidden_for_chat_mode, "quarantined": s.quarantined} for s in all_sections]
            ) != content_hash([s.to_report_section() | {"hidden": s.hidden_for_chat_mode, "quarantined": s.quarantined} for s in after[1]]):
                return _failure(
                    "report_content_changed", "The report changed during reading. Read its current outline before selecting sections again."
                )
            observation = self._observation(canvas.id, observed, source_sections, source_refs, result)
            await self.validate(observation)
            return result, observation
        except Exception as exc:
            logger.warning("report_read_unavailable session=%s failure=%s", self.session_id, type(exc).__name__)
            return _failure("report_reader_unavailable", "Current report access could not be established. The saved report has not been changed.")

    def _observation(self, canvas_id, sections, source_sections, source_refs, result):
        return ReportReadObservation(
            team_id=self.team_id,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            canvas_id=canvas_id,
            sections=[
                {"id": s.id, "rev": s.rev, "content_hash": content_hash(s.to_report_section()), "hidden": s.hidden_for_chat_mode} for s in sections
            ],
            source_sections=source_sections,
            source_refs=source_refs,
            result_hash=content_hash(result),
        )

    async def _pass_read(self, key: tuple[Any, ...], read: Any) -> Any:
        """Read once per validation pass (``report_read_validation_pass``); outside a pass, always read.

        A pass over a conversation's history meets the same report, sections and recorded sources
        once per reference. On 116 (1.7.49) a 24-Turn conversation re-read its ~20k events for every
        Report section reference before each Lead model call, and the Lead waited minutes.
        """
        cache = validation_pass_cache()
        if cache is None:
            return await read()
        key = (*key, self.team_id, self.user_id, self.workspace_id, self.session_id)
        if key not in cache:
            cache[key] = await read()
        return cache[key]

    async def _section_map(self, canvas) -> dict[str, Any]:
        return {s.id: s for s in await self.request.app.state.canvas_store.list_sections(canvas.id, include_hidden=True)}

    async def _session_sources(self) -> list[dict[str, Any]]:
        from bowhead_gateway.routes.canvas import _build_source_refs_for_session

        return await _build_source_refs_for_session(self.request, self.session_id, strict=True)

    async def _accessible(self, canvas, sections, refs) -> bool:
        """Whether every source of these sections and references is readable; one projection per pass."""

        async def project() -> bool:
            projector = await self._sources(canvas, sections, refs)
            return all(projector.access_by_identity.values())

        return await self._pass_read(("sources", content_hash([sections, refs])), project)

    async def _artifact_access(self, identities: list[str]) -> dict[str, bool]:
        """Whether each file is authorized for this report and still present, checked in one batch."""
        from bowhead_gateway.report_artifact_access import authorized_report_artifact_ids

        canvas = await self._pass_read(("canvas",), self._canvas)
        if canvas is None:
            # The same source authority also supports Chats without a report.
            from types import SimpleNamespace

            canvas = SimpleNamespace(user_id=self.user_id, workspace_id=self.workspace_id)
        allowed = await authorized_report_artifact_ids(self.request, canvas, identities, viewer_id=self.user_id)
        present = {a.id for a in await self.request.app.state.artifact_store.get_many([i for i in identities if i in allowed])} if allowed else set()
        return {identity: identity in present for identity in identities}

    async def validate(self, observation):
        if any(getattr(observation, name) != getattr(self, name) for name in ("session_id", "workspace_id", "team_id", "user_id")):
            raise ValueError("Report observation scope changed")
        # A pass over a conversation's history meets the same report once per recorded read. It reads the
        # report once and projects each distinct set of sources once; outside a pass every check reads.
        canvas = await self._pass_read(("canvas",), self._canvas)
        if observation.canvas_id is None:
            # An authorized historical empty report stays an empty observation.
            return
        if canvas is None or canvas.id != observation.canvas_id:
            raise ValueError("Report observation unavailable")
        sections = await self._pass_read(("sections", canvas.id), lambda: self._section_map(canvas))
        for old in observation.sections:
            current = sections.get(old["id"])
            if current is None or current.quarantined or current.hidden_for_chat_mode != old["hidden"]:
                raise ValueError("Report observation unavailable")
        if not await self._accessible(canvas, observation.source_sections, observation.source_refs):
            raise ValueError("Report source unavailable")

    async def authorize_references(self, references):
        """``authorize_reference`` for each reference, in one validation pass; files are checked in one batch."""
        with report_read_validation_pass():
            cache = validation_pass_cache()
            files = list(dict.fromkeys(r.locator["artifact_id"] for r in references if r.kind == "artifact"))
            scope = (self.team_id, self.user_id, self.workspace_id, self.session_id)
            unchecked = [identity for identity in files if ("artifact", identity, *scope) not in cache]
            if unchecked:
                for identity, allowed in (await self._artifact_access(unchecked)).items():
                    cache["artifact", identity, *scope] = allowed
            return [await self.authorize_reference(reference) for reference in references]

    async def authorize_reference(self, reference):
        canvas = await self._pass_read(("canvas",), self._canvas)
        if reference.kind in {"canvas", "canvas_section"}:
            if canvas is None or canvas.id != reference.locator["canvas_id"]:
                return False
            if reference.kind == "canvas":
                return True
            sections = await self._pass_read(("sections", canvas.id), lambda: self._section_map(canvas))
            section = sections.get(reference.locator["section_id"])
            if section is None or section.quarantined:
                return False
            identities = await self._pass_read(("required", section.id, section.rev), lambda: self._required_sources(section))
            refs = await self._pass_read(("session_sources",), self._session_sources) if identities else []
            identities = self._authorizable(identities, self._recorded(refs))
            return await self._accessible(
                canvas, [{"kind": "narrative", "source_refs": identities}], [r for r in refs if canonical_source_identity(r) in identities]
            )
        if reference.kind == "artifact":
            identity = reference.locator["artifact_id"]

            async def check() -> bool:
                return (await self._artifact_access([identity]))[identity]

            return await self._pass_read(("artifact", identity), check)
        # Coach references retain their dedicated current-authority carrier.
        return False

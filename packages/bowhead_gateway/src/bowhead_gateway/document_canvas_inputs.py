"""Authorized Canvas reads for the original private document input adapter."""

from __future__ import annotations

import copy
import uuid
from dataclasses import replace
from types import SimpleNamespace

from bowhead_core._db import open_shared_ctx
from bowhead_core.canvas_export_projection import build_canvas_content_projection
from bowhead_core.documents.authoring_protocol import digest
from bowhead_core.documents.canvas_inputs import CanvasInputAdmission, CanvasInputManifest, CanvasSectionIdentity
from bowhead_core.documents.canvas_material import MAX_BYTES, dataset_record, encode_material
from bowhead_core.documents.request import CanvasInputSelection, DocumentRequestError, DocumentScope
from bowhead_core.documents.store import _authorize
from bowhead_core.results_report_relations import project_report_relations
from bowhead_core.source_access_projection import canonical_source_identity, collect_source_identities
from bowhead_core.source_ref_aggregator import aggregate_source_refs_for_session

from bowhead_gateway.kb_citation_access import kb_file_access_for_user
from bowhead_gateway.source_access_projection import ViewerSafeCanvasProjector, ViewerSourceProjectionError, _source_objects


class DocumentCanvasInputs:
    def __init__(self, state):
        self.state = state

    async def _actor(self, scope: DocumentScope):
        async with open_shared_ctx(self.state.artifact_store.db_path) as db:
            await _authorize(db, scope, write=False)
        user = await self.state.user_store.get_user(scope.user_id)
        if user is None:
            raise DocumentRequestError("document_not_found")
        # The original Space binding supplies the Team; the legacy user field
        # cannot replace that binding in current KB permission checks.
        return replace(user, team_id=scope.team_id)

    async def _projector(self, *, scope, canvas_id, session_id, sections, source_refs, closure):
        user = await self._actor(scope)
        store = getattr(self.state, "canvas_store", None)
        canvas = await store.get_canvas(canvas_id) if store else None
        if canvas is None or (canvas.session_id, canvas.user_id, canvas.workspace_id) != (session_id, scope.user_id, scope.workspace_id):
            raise DocumentRequestError("document_canvas_unavailable")
        try:
            return await ViewerSafeCanvasProjector.build(
                root_secret=getattr(self.state, "artifact_fetch_secret", None),
                workspace_id=scope.workspace_id,
                session_id=session_id,
                sections=sections,
                source_refs=source_refs,
                report_artifact_closure=closure,
                artifact_store=self.state.artifact_store,
                kb_file_access=kb_file_access_for_user(SimpleNamespace(state=self.state), user),
            )
        except ViewerSourceProjectionError as error:
            raise DocumentRequestError("document_canvas_access_unavailable") from error

    async def capture(self, *, scope: DocumentScope, session_id: str, selection: CanvasInputSelection) -> tuple[CanvasInputManifest, bytes]:
        await self._actor(scope)
        store = getattr(self.state, "canvas_store", None)
        canvas = await store.get_canvas_by_session(session_id) if store else None
        if canvas is None:
            raise DocumentRequestError("document_canvas_unavailable")
        stable = await store.read_canvas_projection_state(canvas.id)
        if stable is None:
            raise DocumentRequestError("document_canvas_unavailable")
        canvas, sections = stable
        if (canvas.session_id, canvas.user_id, canvas.workspace_id) != (session_id, scope.user_id, scope.workspace_id):
            raise DocumentRequestError("document_canvas_unavailable")
        projection = build_canvas_content_projection(title=canvas.title, sections=sections)
        visible_ids = {item.section_id for item in projection.sections if not item.quarantined}
        selected = [
            section for section in sections if section.id in visible_ids and (selection.section_ids is None or section.id in selection.section_ids)
        ]
        if not selected or len(selected) > 64 or (selection.section_ids is not None and len(selected) != len(selection.section_ids)):
            raise DocumentRequestError("document_canvas_selection_invalid")
        canonical = [section.to_report_section() for section in selected]
        identities = set(collect_source_identities(canonical))
        session_store = getattr(self.state, "session_store", None)
        log_store = getattr(self.state, "turn_log_store", None)
        refs = (
            await aggregate_source_refs_for_session(session_id, session_store=session_store, turn_log_store=log_store)
            if session_store is not None and log_store is not None
            else []
        )
        related = [ref for ref in refs if canonical_source_identity(ref) in identities]
        # This is the original report byte-reader closure, not a chart-edit
        # provenance predicate or a model-authored list of authorized files.
        closure = tuple(sorted(project_report_relations(selected, source_refs=related).closure_ids))
        resource_refs = [{"id": artifact_id, "artifact_id": artifact_id} for artifact_id in closure]
        projector = await self._projector(
            scope=scope,
            canvas_id=canvas.id,
            session_id=session_id,
            sections=canonical,
            source_refs=[*related, *resource_refs],
            closure=closure,
        )
        projected, _ = projector.project(canonical)
        datasets = {}
        aliases = {}
        artifacts = {artifact.id: artifact for artifact in await self.state.artifact_store.get_many(list(closure))} if closure else {}
        for index, artifact_id in enumerate(closure):
            alias = f"dataset-{index}"
            aliases[artifact_id] = alias
            artifact = artifacts.get(artifact_id)
            if artifact is None or artifact.status != "ready" or not projector.access_by_identity.get(artifact_id, False):
                datasets[alias] = {"availability": "unavailable"}
                continue
            try:
                content = await self.state.artifact_store.backend.read_bounded(artifact.path, limit=MAX_BYTES)
            except FileNotFoundError:
                datasets[alias] = {"availability": "unavailable"}
                continue
            datasets[alias] = {"availability": "available", **dataset_record(filename=artifact.filename, mime=artifact.mime, content=content)}
        projected = copy.deepcopy(projected)
        for section in projected:
            spec = section.get("spec")
            if isinstance(spec, dict):
                views = [spec, *(view["spec"] for view in spec.get("views", []) if isinstance(view, dict) and isinstance(view.get("spec"), dict))]
                for view in views:
                    artifact_id = view.pop("data_artifact_id", None)
                    if artifact_id is not None:
                        view["data_input_alias"] = aliases.get(artifact_id)
        content = encode_material(
            canvas_id=canvas.id, session_id=session_id, projection_token=projection.projection_token, sections=projected, datasets=datasets
        )
        manifest = CanvasInputManifest(
            canvas_id=canvas.id,
            session_id=session_id,
            projection_token=projection.projection_token,
            sections=tuple(CanvasSectionIdentity(section_id=section.id, rev=section.rev) for section in selected),
            source_refs=tuple(dict(ref) for ref in _source_objects(canonical, [*related, *resource_refs])),
            artifact_closure=closure,
            accessible_sources=tuple(sorted(identity for identity, allowed in projector.access_by_identity.items() if allowed)),
            sha256=digest(content),
            size_bytes=len(content),
        )
        # A source disappearing during byte reads cannot become a ready input.
        await self.authorize(scope=scope, manifest=manifest)
        return manifest, content

    async def prepare(self, *, scope: DocumentScope, session_id: str, selection: CanvasInputSelection) -> CanvasInputAdmission:
        manifest, _ = await self.capture(scope=scope, session_id=session_id, selection=selection)
        return CanvasInputAdmission(
            manifest=manifest,
            artifact_id="art_" + uuid.uuid4().hex,
            path=self.state.artifact_store.backend.immutable_upload_path(uuid.uuid4().hex),
        )

    async def materialize(self, *, scope: DocumentScope, manifest: CanvasInputManifest) -> bytes:
        current, content = await self.capture(
            scope=scope,
            session_id=manifest.session_id,
            selection=CanvasInputSelection(section_ids=tuple(section.section_id for section in manifest.sections)),
        )
        if current != manifest:
            raise DocumentRequestError("document_canvas_changed")
        return content

    async def authorize(self, *, scope: DocumentScope, manifest: CanvasInputManifest) -> None:
        # The original immutable job manifest supplies the retained relationship.
        # Current resource checks still run; source aliases and cached grants do
        # not authorize it. No historical section is presented as current.
        projector = await self._projector(
            scope=scope,
            canvas_id=manifest.canvas_id,
            session_id=manifest.session_id,
            sections=[],
            source_refs=list(manifest.source_refs),
            closure=manifest.artifact_closure,
        )
        if any(not projector.access_by_identity.get(identity, False) for identity in manifest.accessible_sources):
            raise DocumentRequestError("document_canvas_source_unavailable")

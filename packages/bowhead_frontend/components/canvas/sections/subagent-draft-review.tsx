"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { localizedToolText } from "@/lib/tool-display";
import { useAppRouter } from "@/lib/use-app-router";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { handoffAgentToNewChat } from "@/lib/composer-handoff";

import { PromptBudgetHint } from "@/components/agents/prompt-budget-hint";
import { MAX_PROMPT_CHARS_TEAM } from "@/lib/agent-prompt-budget";
import {
  clarifySubagentDraft,
  convertSubagentDraftToManual,
  createSubagentFromDraft,
  discardSubagentDraft,
  getSubagentDraft,
  regenerateSubagentDraft,
  SubagentDraftApiError,
  updateSubagentDraft,
  type CreatedSubagentFromDraft,
  type ManualSubagentDraftDefinition,
  type ManualSubagentDraftDetail,
  type SubagentDraftDefinition,
  type SubagentDraftDetail,
  type SubagentDraftTool,
} from "@/lib/subagent-drafts-api";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { subagentDraftFailureKey } from "./subagent-draft-errors";
import { DialogShell } from "@/components/ui/dialog";
import { blockedItemLines, dependencyIssueItems, parseBlockedItems } from "@/lib/agent-blocked-items";
import { isSkillOperationToolRef, toolSelectionLockedReasonKey, withoutSkillOperationTools } from "@/lib/agent-tool-selection";
import {
  getAgentAuthoringCatalog,
  type AgentAuthoringPluginSection,
  type AgentAuthoringSkill,
  type AgentAuthoringTool,
  type AgentExactSkillGrant,
} from "@/lib/agents-api";
import { ResourcePluginPicker, ResourceSkillPicker, pluginsFromCatalog } from "@/components/ui/resource-picker";
import { CharIcon } from "@/components/char-icon";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import { requestConfirm } from "@/lib/confirm-dialog-store";

type Props = {
  draftId: string;
  initialDraft?: SubagentDraftDetail | null;
  embedded?: boolean;
  refreshVersion?: number | null;
  onDraftChange?: (binding: { draftId: string; version: number; dirty: boolean } | null) => void;
  chatDraftEditEnabled?: boolean;
  onClose: () => void;
  onCreated?: (result: CreatedSubagentFromDraft) => void;
  unifiedDraftEnabled?: boolean;
  onManual?: (purposeHint: string | null, converted?: ManualSubagentDraftDetail) => void;
  onActivate?: () => void;
  viewRef?: React.RefObject<{ requestClose: () => void } | null>;
};

type EditableDefinition = Omit<SubagentDraftDefinition, "visibility" | "tool_refs" | "skills">;

function editableDefinition(
  definition: SubagentDraftDefinition,
): EditableDefinition {
  return {
    name: definition.name,
    display_name: definition.display_name,
    description: definition.description,
    system_prompt: definition.system_prompt,
    model: definition.model,
    max_iterations: definition.max_iterations,
    output_schema: definition.output_schema,
  };
}

type DraftResources = {
  skill_grants: AgentExactSkillGrant[];
  capability_ids: string[];
  builtin_plugin_ids: string[];
};

function draftResources(definition: SubagentDraftDefinition | null | undefined): DraftResources {
  return {
    skill_grants: definition?.skill_grants ?? [],
    capability_ids: definition?.capability_ids ?? [],
    builtin_plugin_ids: definition?.builtin_plugin_ids ?? [],
  };
}

/** Definition fields for the resources; `builtin_plugin_ids` only when something is selected. */
function resourceFields(resources: DraftResources): Pick<SubagentDraftDefinition, "skill_grants" | "capability_ids" | "builtin_plugin_ids"> {
  return {
    skill_grants: resources.skill_grants,
    capability_ids: resources.capability_ids,
    ...(resources.builtin_plugin_ids.length ? { builtin_plugin_ids: resources.builtin_plugin_ids } : {}),
  };
}

function isRisky(tool: SubagentDraftTool): boolean {
  return tool.risk === "external_write" || tool.risk === "destructive";
}


export function SubagentDraftReview({
  draftId,
  initialDraft = null,
  embedded = false,
  refreshVersion = null,
  onDraftChange,
  chatDraftEditEnabled = Boolean(onDraftChange),
  onClose: disposeView,
  onCreated,
  unifiedDraftEnabled = false,
  onManual,
  onActivate,
  viewRef,
}: Props) {
  const t = useTranslations("settings.subagents");
  const tCreate = useTranslations("create_subagent");
  const tPicker = useTranslations("resource_picker");
  const tMarket = useTranslations("agents_marketplace");
  const locale = useLocale();
  const tRef = useRef(t);
  tRef.current = t;
  const router = useAppRouter();
  const canDirectUse = useOptionalAuthSession()?.me?.capabilities?.composer?.agent_start_use === true;
  const nameId = useId();
  const displayNameId = useId();
  const descriptionId = useId();
  const promptId = useId();
  const clarificationId = useId();
  const requestTokenRef = useRef(0);
  const dirtyRef = useRef(false);
  const localGeneration = useRef(0);
  const loadControllerRef = useRef<AbortController | null>(null);
  const [draft, setDraft] = useState<SubagentDraftDetail | null>(
    initialDraft?.draft_id === draftId ? initialDraft : null,
  );
  const [definition, setDefinition] = useState<EditableDefinition | null>(
    initialDraft?.definition ? editableDefinition(initialDraft.definition) : null,
  );
  const [selectedTools, setSelectedTools] = useState<Set<string>>(
    () => new Set(
      initialDraft?.definition?.tool_refs
        ?? initialDraft?.tools.filter((tool) => tool.selected).map((tool) => tool.tool_ref)
        ?? [],
    ),
  );
  const [resources, setResources] = useState<DraftResources>(() => draftResources(initialDraft?.definition));
  const [catalogPlugins, setCatalogPlugins] = useState<AgentAuthoringPluginSection | undefined>(undefined);
  const [catalogSkills, setCatalogSkills] = useState<AgentAuthoringSkill[] | null>(null);
  // A failed load shows an actionable state instead of silently dropping the pickers (§7.3 item 7).
  const [pickerLoadError, setPickerLoadError] = useState<ShownError | null>(null);
  const [pickerReload, setPickerReload] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [clarificationAnswer, setClarificationAnswer] = useState(
    initialDraft?.clarification?.answer ?? "",
  );
  const [acknowledged, setAcknowledged] = useState<Set<string>>(new Set());
  const [busyAction, setBusyAction] = useState<
    "loading" | "clarifying" | "saving" | "creating" | "discarding" | "manualizing" | null
  >(initialDraft?.draft_id === draftId ? null : "loading");
  // Show only localized explanations; raw API messages, status codes and request IDs go into Copy Diagnostics (outside Notice's alert).
  const [error, setErrorState] = useState<ShownError | null>(null);
  const setError = useCallback((message: string | null) => {
    setErrorState(message === null ? null : shownError(message));
  }, []);
  const tMarketRef = useRef(tMarket);
  tMarketRef.current = tMarket;
  const showFailure = useCallback((cause: unknown) => {
    // A dependency block names each Skill or Tool and its reason instead of the generic validation copy.
    const blocked = cause instanceof SubagentDraftApiError ? parseBlockedItems(cause.details) : [];
    const message = blocked.length
      ? blockedItemLines(blocked, (key, values) => tMarketRef.current(key as never, values as never)).join("\n")
      : tRef.current(subagentDraftFailureKey(cause));
    setErrorState(shownError(message, cause));
  }, []);
  const [conflict, setConflict] = useState(false);
  const [expired, setExpired] = useState<{
    canRegenerate: boolean;
    etag: string | null;
  } | null>(null);
  const [created, setCreated] = useState<CreatedSubagentFromDraft | null>(null);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [catalogTools, setCatalogTools] = useState<AgentAuthoringTool[] | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState(false);
  const [toolQuery, setToolQuery] = useState("");
  const lifecycle = useRef({ dirty, busyAction, draft, disposeView, onActivate, onDraftChange });
  lifecycle.current = { dirty, busyAction, draft, disposeView, onActivate, onDraftChange };
  const legacyLeaveRef = useRef<(() => Promise<boolean>) | null>(null);
  const onClose = () => { void legacyLeaveRef.current?.().then((allowed) => { if (allowed) lifecycle.current.disposeView(); }); };
  useEffect(() => {
    let disposed = false;
    let confirmation: AbortController | null = null;
    const writing = () => ["saving", "creating", "discarding", "manualizing", "clarifying"].includes(lifecycle.current.busyAction ?? "");
    const hasUnsaved = () => dirtyRef.current || writing();
    const check = async () => {
      if (writing()) return false;
      if (!dirtyRef.current) return true;
      lifecycle.current.onActivate?.();
      const originalDraft = lifecycle.current.draft;
      const originalGeneration = localGeneration.current;
      confirmation?.abort();
      confirmation = new AbortController();
      const allowed = await requestConfirm({ title: tCreate("draft_leave_title"), description: tCreate("draft_leave_description"), confirmLabel: tCreate("draft_discard_changes"), cancelLabel: tCreate("draft_continue_editing"), tone: "primary" }, confirmation.signal);
      if (!allowed || disposed || writing() || lifecycle.current.draft !== originalDraft || localGeneration.current !== originalGeneration) return false;
      // Legacy compatibility is explicit; no conversion or model call is made on disposal.
      dirtyRef.current = false;
      setDirty(false);
      if (originalDraft?.definition) setDefinition(editableDefinition(originalDraft.definition));
      setSelectedTools(new Set(originalDraft?.definition?.tool_refs ?? []));
      setResources(draftResources(originalDraft?.definition));
      setClarificationAnswer(originalDraft?.clarification?.answer ?? "");
      lifecycle.current.onDraftChange?.(originalDraft && ["ready", "blocked"].includes(originalDraft.draft_status) ? { draftId: originalDraft.draft_id, version: originalDraft.version, dirty: false } : null);
      return true;
    };
    legacyLeaveRef.current = check;
    const view = { requestClose: () => { void check().then((allowed) => { if (allowed && !disposed) lifecycle.current.disposeView(); }); } };
    if (viewRef) viewRef.current = view;
    const unregister = registerSubagentDraftNavigationGuard({ hasUnsaved, confirmLeave: check, cancelLeave: () => confirmation?.abort() });
    const warn = (event: BeforeUnloadEvent) => { if (hasUnsaved()) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", warn);
    return () => { disposed = true; confirmation?.abort(); unregister(); legacyLeaveRef.current = null; window.removeEventListener("beforeunload", warn); if (viewRef?.current === view) viewRef.current = null; };
  }, [draftId, viewRef, tCreate]);

  const adoptDraft = useCallback((next: SubagentDraftDetail) => {
    setDraft(next);
    setClarificationAnswer((current) => (
      next.clarification?.answer
      ?? (next.draft_status === "needs_clarification" ? current : "")
    ));
    setDefinition(
      next.definition ? editableDefinition(next.definition) : null,
    );
    setSelectedTools(
      new Set(
        next.definition?.tool_refs
          ?? next.tools.filter((tool) => tool.selected).map((tool) => tool.tool_ref),
      ),
    );
    setResources(draftResources(next.definition));
    setAcknowledged(new Set());
    setDirty(false);
    dirtyRef.current = false;
    setConflict(false);
    setExpired(null);
    setError(null);
    onDraftChange?.(next.draft_status === "ready" || next.draft_status === "blocked"
      ? { draftId: next.draft_id, version: next.version, dirty: false } : null);
  }, [onDraftChange]);

  const load = useCallback(async () => {
    const token = requestTokenRef.current + 1;
    requestTokenRef.current = token;
    loadControllerRef.current?.abort();
    const controller = new AbortController();
    loadControllerRef.current = controller;
    setBusyAction("loading");
    try {
      const next = await getSubagentDraft(draftId, controller.signal);
      if (requestTokenRef.current === token) {
        if (dirtyRef.current) {
          setConflict(true);
          setError(tRef.current("draft_conflict"));
        } else adoptDraft(next);
      }
    } catch (nextError) {
      if (requestTokenRef.current !== token) return;
      if (
        nextError instanceof SubagentDraftApiError
        && nextError.status === 410
      ) {
        setExpired({
          canRegenerate: nextError.details?.can_regenerate === true,
          etag: (
            typeof nextError.details?.etag === "string"
              ? nextError.details.etag
              : null
          ),
        });
      } else if (
        !(nextError instanceof DOMException && nextError.name === "AbortError")
      ) {
        showFailure(nextError);
      }
    } finally {
      if (requestTokenRef.current === token) {
        loadControllerRef.current = null;
        setBusyAction(null);
      }
    }
  }, [adoptDraft, draftId]);

  useEffect(() => {
    if (initialDraft?.draft_id === draftId) {
      adoptDraft(initialDraft);
      return;
    }
    void load();
    return () => {
      requestTokenRef.current += 1;
      loadControllerRef.current?.abort();
      loadControllerRef.current = null;
    };
  }, [adoptDraft, draftId, initialDraft, load]);

  useEffect(() => {
    if (refreshVersion === null || !draft || refreshVersion <= draft.version) return;
    if (dirty) {
      setConflict(true);
      setError(tRef.current("draft_conflict"));
      return;
    }
    void load();
  }, [draft, dirty, load, refreshVersion]);

  const setField = <K extends keyof EditableDefinition>(
    field: K,
    value: EditableDefinition[K],
  ) => {
    setDefinition((current) => (
      current ? { ...current, [field]: value } : current
    ));
    setDirty(true);
    dirtyRef.current = true;
    localGeneration.current += 1;
    if (draft) onDraftChange?.({ draftId: draft.draft_id, version: draft.version, dirty: true });
  };

  const toolsByTier = useMemo(() => {
    const tools = (draft?.tools ?? []).filter((tool) => !isSkillOperationToolRef(tool.tool_ref));
    return {
      required: tools.filter((tool) => tool.tier === "required" && !isRisky(tool)),
      recommended: tools.filter((tool) => tool.tier !== "required" && !isRisky(tool)),
      risky: tools.filter(isRisky),
    };
  }, [draft?.tools]);

  const updateTool = (tool: SubagentDraftTool, checked: boolean) => {
    if (tool.locked_reason && checked) return;
    setSelectedTools((current) => {
      const next = new Set(current);
      if (checked) next.add(tool.tool_ref);
      else next.delete(tool.tool_ref);
      return next;
    });
    setDirty(true);
    dirtyRef.current = true;
    localGeneration.current += 1;
    if (draft) onDraftChange?.({ draftId: draft.draft_id, version: draft.version, dirty: true });
  };

  const openToolCatalog = async (refresh = false) => {
    setCatalogOpen(true);
    if ((!refresh && catalogTools !== null) || catalogLoading) return;
    setCatalogLoading(true);
    setCatalogError(false);
    try {
      const catalog = await getAgentAuthoringCatalog();
      setCatalogTools(withoutSkillOperationTools(catalog.tools));
      setCatalogPlugins(catalog.plugins);
      setCatalogSkills(catalog.skills);
    } catch {
      setCatalogError(true);
    } finally {
      setCatalogLoading(false);
    }
  };

  const updateCatalogTool = (tool: AgentAuthoringTool, checked: boolean) => {
    if (checked && !tool.selectable) return;
    setSelectedTools((current) => {
      const next = new Set(current);
      if (checked) next.add(tool.stable_ref);
      else next.delete(tool.stable_ref);
      return next;
    });
    setDirty(true);
    dirtyRef.current = true;
    localGeneration.current += 1;
    if (draft) onDraftChange?.({ draftId: draft.draft_id, version: draft.version, dirty: true });
  };

  // The Plugin and Skill pickers need the catalog up front; Tools keep loading it on demand.
  useEffect(() => {
    let active = true;
    setPickerLoadError(null);
    void Promise.resolve(getAgentAuthoringCatalog())
      .then((catalog) => {
        if (!active || !catalog) return;
        setCatalogPlugins(catalog.plugins);
        setCatalogSkills(catalog.skills);
      })
      .catch((caught: unknown) => {
        // Saved resources are kept as they are; the picker says so and offers a retry.
        if (active) setPickerLoadError(shownError(tPicker("catalog_failed_title"), caught));
      });
    return () => { active = false; };
  }, [pickerReload, tPicker]);

  const updateResources = (change: (current: DraftResources) => DraftResources) => {
    setResources(change);
    setDirty(true);
    dirtyRef.current = true;
    localGeneration.current += 1;
    if (draft) onDraftChange?.({ draftId: draft.draft_id, version: draft.version, dirty: true });
  };

  const removeUnavailableTool = (toolRef: string) => {
    setSelectedTools((current) => {
      const next = new Set(current);
      next.delete(toolRef);
      return next;
    });
    setDirty(true);
    dirtyRef.current = true;
    localGeneration.current += 1;
    if (draft) onDraftChange?.({ draftId: draft.draft_id, version: draft.version, dirty: true });
  };

  const otherCatalogTools = useMemo(() => {
    const recommended = new Set(draft?.tools.map((tool) => tool.tool_ref) ?? []);
    const query = toolQuery.trim().toLocaleLowerCase();
    return (catalogTools ?? [])
      .filter((tool) => !recommended.has(tool.stable_ref))
      .filter((tool) => !query || [localizedToolText(tool, locale).name, localizedToolText(tool, locale).description, tool.display_name, tool.display_name_en, tool.stable_ref]
        .some((value) => value?.toLocaleLowerCase().includes(query)))
      .sort((a, b) => {
        const selectedOrder = Number(selectedTools.has(b.stable_ref)) - Number(selectedTools.has(a.stable_ref));
        if (selectedOrder) return selectedOrder;
        const name = (tool: AgentAuthoringTool) => localizedToolText(tool, locale).name;
        return name(a).localeCompare(name(b), locale);
      });
  }, [catalogTools, draft?.tools, locale, selectedTools, toolQuery]);
  const extraSelectedTools = useMemo(() => {
    const recommended = new Set(draft?.tools.map((tool) => tool.tool_ref) ?? []);
    return (catalogTools ?? []).filter((tool) => selectedTools.has(tool.stable_ref) && !recommended.has(tool.stable_ref));
  }, [catalogTools, draft?.tools, selectedTools]);
  const unavailableSelectedRefs = useMemo(() => {
    if (catalogTools === null) return [];
    const visible = new Set([
      ...(draft?.tools.map((tool) => tool.tool_ref) ?? []),
      ...catalogTools.map((tool) => tool.stable_ref),
    ]);
    return [...selectedTools].filter((toolRef) => !visible.has(toolRef) && !isSkillOperationToolRef(toolRef));
  }, [catalogTools, draft?.tools, selectedTools]);

  const save = async (): Promise<SubagentDraftDetail | null> => {
    if (!draft || !definition || !draft.actions.edit) return null;
    setBusyAction("saving");
    setError(null);
    setConflict(false);
    try {
      const next = await updateSubagentDraft(
        draft.draft_id,
        {
          schema_version: 1,
          definition: {
            ...definition,
            ...resourceFields(resources),
            visibility: "creator_only",
          },
          selected_tool_refs: Array.from(selectedTools).sort(),
          selected_skills: [],
        },
        draft.etag,
      );
      adoptDraft(next);
      return next;
    } catch (nextError) {
      if (
        nextError instanceof SubagentDraftApiError
        && nextError.code === "subagent_draft_version_conflict"
      ) {
        setConflict(true);
        setError(t("draft_conflict"));
      } else {
        showFailure(nextError);
      }
      return null;
    } finally {
      setBusyAction(null);
    }
  };

  const clarify = async () => {
    if (!draft || !draft.actions.clarify || !clarificationAnswer.trim()) return;
    setBusyAction("clarifying");
    setError(null);
    try {
      const next = await clarifySubagentDraft(
        draft.draft_id,
        clarificationAnswer.trim(),
        draft.etag,
      );
      setClarificationAnswer("");
      adoptDraft(next);
    } catch (nextError) {
      try {
        const latest = await getSubagentDraft(draft.draft_id);
        adoptDraft(latest);
        if (latest.draft_status === "blocked") {
          setError(t("draft_generation_failed"));
        } else if (latest.draft_status === "needs_clarification") {
          showFailure(nextError);
        }
      } catch {
        setConflict(true);
        setError(t("draft_conflict"));
      }
    } finally {
      setBusyAction(null);
    }
  };

  const create = async () => {
    if (!draft) return;
    setError(null);
    let current = draft;
    if (dirty) {
      const saved = await save();
      if (!saved) return;
      current = saved;
      // Saving adopts a new snapshot and clears acknowledgements. Do not use
      // this callback's pre-save Set to confirm the new definition.
      if (current.actions.create && current.warnings.length > 0) {
        setError(t("draft_ack_required"));
        return;
      }
    }
    if (!current.actions.create) {
      setError(t("draft_fix_validation"));
      return;
    }
    const warningCodes = current.warnings
      .map((warning) => warning.code)
      .filter(Boolean);
    if (warningCodes.some((code) => !acknowledged.has(code))) {
      setError(t("draft_ack_required"));
      return;
    }
    setBusyAction("creating");
    try {
      const result = await createSubagentFromDraft(
        current,
        warningCodes,
      );
      setCreated(result);
      onCreated?.(result);
    } catch (nextError) {
      if (
        nextError instanceof SubagentDraftApiError
        && (
          nextError.code === "subagent_catalog_changed"
          || nextError.code === "subagent_draft_version_conflict"
        )
      ) {
        setConflict(true);
      }
      showFailure(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const discard = async () => {
    if (!draft || !draft.actions.discard) {
      onClose();
      return;
    }
    setBusyAction("discarding");
    setError(null);
    try {
      await discardSubagentDraft(draft.draft_id, draft.etag);
      onClose();
    } catch (nextError) {
      showFailure(nextError);
      setBusyAction(null);
    }
  };

  const regenerate = async () => {
    if (!expired?.canRegenerate || !expired.etag) return;
    setBusyAction("loading");
    setError(null);
    try {
      adoptDraft(await regenerateSubagentDraft(draftId, expired.etag));
    } catch (nextError) {
      showFailure(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const moveToManual = async (purposeHint: string | null) => {
    if (!onManual) return;
    if (!unifiedDraftEnabled || !draft || expired) {
      onManual(purposeHint);
      return;
    }
    const purpose = purposeHint ?? "";
    const manualDefinition: ManualSubagentDraftDefinition = definition ? {
      ...definition,
      ...resourceFields(resources),
      visibility: "creator_only",
      tool_refs: [...selectedTools].sort(),
      output_schema_text: definition.output_schema
        ? JSON.stringify(definition.output_schema, null, 2) : "",
    } : {
      name: "",
      display_name: "",
      description: purpose.slice(0, 200),
      system_prompt: purpose.slice(0, MAX_PROMPT_CHARS_TEAM),
      model: "analyst",
      max_iterations: 12,
      output_schema: null,
      output_schema_text: "",
      visibility: "creator_only",
      tool_refs: [],
    };
    setBusyAction("manualizing");
    setError(null);
    try {
      const converted = await convertSubagentDraftToManual(
        draft, purpose, manualDefinition,
        selectedTools.size === 0 && draft.warnings.some((warning) => (
          warning.code === "subagent_text_only_confirmation"
        )),
      );
      onDraftChange?.(null);
      onManual(purposeHint, converted);
    } catch (nextError) {
      if (nextError instanceof SubagentDraftApiError && nextError.status === 409) {
        setConflict(true);
        setError(t("draft_conflict"));
      } else {
        showFailure(nextError);
      }
    } finally {
      setBusyAction(null);
    }
  };

  if (created) {
    return (
      <CardShell title={t("draft_success_title")} onClose={onClose} embedded={embedded}>
        <div role="status" style={{ display: "grid", gap: 14 }}>
          <div
            style={{
              border: "1px solid var(--c-accent-line)",
              borderRadius: 8,
              background: "var(--c-accent-tint)",
              padding: 16,
              color: "var(--c-ink)",
            }}
          >
            <div style={{ fontSize: "0.9375rem", fontWeight: 650 }}>
              {definition?.display_name || definition?.name}
            </div>
            <div style={{ marginTop: 5, fontSize: "0.75rem", color: "var(--c-ink-3)" }}>
              {t(canDirectUse ? "draft_success_body_direct" : "draft_success_body")}
            </div>
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            {canDirectUse ? <Button variant="primary" onClick={() => handoffAgentToNewChat({ agentId: created.definition_id, label: definition?.display_name || definition?.name || created.definition_id })}>
              {t("draft_start_use")}
            </Button> : null}
            <Button onClick={() => router.push(`/agents/manage/?agent_id=${encodeURIComponent(created.definition_id)}`)}>
              {t("draft_manage_agent")}
            </Button>
            <Button onClick={onClose}>
              {t("draft_done")}
            </Button>
          </div>
        </div>
      </CardShell>
    );
  }

  if (expired) {
    return (
      <CardShell title={t("draft_expired_title")} onClose={onClose} embedded={embedded}>
        <Notice tone="muted">{t("draft_expired_body")}</Notice>
        {error ? <Notice tone="error">{error.message}</Notice> : null}
        {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
        <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
          {onManual ? (
            <Button onClick={() => void moveToManual(draft?.purpose ?? null)}>
              {t("manual_create")}
            </Button>
          ) : <span />}
          <div style={{ display: "flex", gap: 8 }}>
            <Button onClick={onClose}>
              {t("draft_done")}
            </Button>
            {expired.canRegenerate && expired.etag ? (
              <Button
                variant="primary"
                disabled={busyAction !== null}
                onClick={() => void regenerate()}
              >
                {busyAction === "loading"
                  ? t("draft_generating")
                  : t("draft_regenerate")}
              </Button>
            ) : null}
          </div>
        </div>
      </CardShell>
    );
  }

  if (!draft && busyAction !== "loading") {
    return (
      <CardShell title={t("draft_review_title")} onClose={onClose} embedded={embedded}>
        {error ? <Notice tone="error">{error.message}</Notice> : null}
        {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
        <Button onClick={() => void load()}>{t("draft_reload")}</Button>
      </CardShell>
    );
  }

  if (!draft || busyAction === "loading") {
    return (
      <CardShell title={t("draft_review_title")} onClose={onClose} embedded={embedded} busy>
        <div role="status" aria-live="polite" style={{ color: "var(--c-ink-3)", fontSize: "0.8125rem" }}>
          {t("draft_loading")}
        </div>
        {error ? <Notice tone="error">{error.message}</Notice> : null}
        {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
      </CardShell>
    );
  }

  const clarification = draft.clarification;
  const manualHint = [draft.purpose, clarificationAnswer.trim()]
    .filter(Boolean)
    .join("\n")
    .slice(0, 2000) || null;
  if (draft.draft_status === "generating") {
    return (
      <CardShell title={t("draft_review_title")} onClose={onClose} embedded={embedded}>
        <Notice tone="muted">{t("draft_generating")}</Notice>
        <div style={{ display: "flex", gap: 8 }}>
          <Button onClick={() => void load()}>{t("draft_reload")}</Button>
          {onManual ? <Button disabled={busyAction !== null} onClick={() => void moveToManual(manualHint)}>{t("manual_create")}</Button> : null}
        </div>
      </CardShell>
    );
  }
  if (draft.draft_status === "needs_clarification" && clarification) {
    return (
      <CardShell
        title={t("draft_clarify_title")}
        onClose={onClose}
        embedded={embedded}
        busy={busyAction === "clarifying"}
      >
        <Notice tone="muted">{t(chatDraftEditEnabled && draft && ["ready", "blocked"].includes(draft.draft_status) ? (dirty ? "draft_chat_save_first" : "draft_chat_bound") : "draft_chat_unbound")}</Notice>
        <p style={{ margin: 0, color: "var(--c-ink-2)", fontSize: "0.875rem", lineHeight: 1.65 }}>
          {clarification.question}
        </p>
        <Field
          id={clarificationId}
          label={t("draft_clarify_label")}
          hint={t("draft_clarify_hint")}
        >
          <textarea
            id={clarificationId}
            className="settings-input"
            rows={4}
            maxLength={2000}
            value={clarificationAnswer}
            onChange={(event) => setClarificationAnswer(event.target.value)}
            placeholder={t("draft_clarify_placeholder")}
          />
        </Field>
        {error ? <Notice tone="error">{error.message}</Notice> : null}
        {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
        {conflict ? (
          <Button onClick={() => void load()} disabled={busyAction !== null}>
            {t("draft_reload")}
          </Button>
        ) : null}
        <div style={{ display: "flex", gap: 8, justifyContent: "space-between", flexWrap: "wrap" }}>
          {onManual ? (
            <Button
              onClick={() => void moveToManual(manualHint)}
              disabled={busyAction !== null}
            >
              {t("manual_create")}
            </Button>
          ) : <span />}
          <div style={{ display: "flex", gap: 8 }}>
            <Button onClick={() => void discard()} disabled={busyAction !== null}>
              {t("draft_discard")}
            </Button>
            <Button
              variant="primary"
              disabled={busyAction !== null || conflict || !clarificationAnswer.trim()}
              onClick={() => void clarify()}
            >
              {busyAction === "clarifying" ? t("draft_generating") : t("draft_clarify_submit")}
            </Button>
          </div>
        </div>
      </CardShell>
    );
  }

  if (!definition) {
    return (
      <CardShell title={t("draft_blocked_title")} onClose={onClose} embedded={embedded}>
        <Notice tone="error">{t("draft_blocked_body")}</Notice>
        {error ? <Notice tone="error">{error.message}</Notice> : null}
        {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
        <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
          {onManual ? (
            <Button disabled={busyAction !== null} onClick={() => void moveToManual(manualHint)}>
              {t("manual_create")}
            </Button>
          ) : <span />}
          <Button onClick={() => void discard()}>
            {t("draft_discard")}
          </Button>
        </div>
      </CardShell>
    );
  }

  const allWarningsAcknowledged = draft.warnings.every(
    (warning) => acknowledged.has(warning.code),
  );
  const isBusy = busyAction !== null;

  return (
    <CardShell
      title={t("draft_review_title")}
      onClose={onClose}
      embedded={embedded}
      busy={isBusy}
    >
      <Notice tone="muted">{t(chatDraftEditEnabled && draft && ["ready", "blocked"].includes(draft.draft_status) ? (dirty ? "draft_chat_save_first" : "draft_chat_bound") : "draft_chat_unbound")}</Notice>
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          gap: 12,
          paddingBottom: 14,
          borderBottom: "1px solid var(--c-line)",
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: "1.0625rem", fontWeight: 650, lineHeight: 1.3 }}>
            {definition.display_name || definition.name}
          </div>
          <div style={{ marginTop: 5, fontSize: "0.78125rem", color: "var(--c-ink-3)", lineHeight: 1.55 }}>
            {draft.goal_summary || definition.description}
          </div>
        </div>
        <span
          style={{
            flexShrink: 0,
            border: "1px solid var(--c-info-line)",
            borderRadius: 5,
            background: "var(--c-info-soft)",
            color: "var(--c-info-ink)",
            fontSize: "0.65625rem",
            fontWeight: 650,
            padding: "2px 7px",
          }}
        >
          {t("vis_creator_only")}
        </span>
      </div>

      {draft.assumptions.length > 0 ? (
        <Section title={t("draft_assumptions")}>
          <ul style={{ margin: 0, paddingLeft: 18, color: "var(--c-ink-2)", fontSize: "0.78125rem", lineHeight: 1.65 }}>
            {draft.assumptions.slice(0, 3).map((assumption) => (
              <li key={assumption}>{assumption}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      <Section title={t("draft_capabilities")}>
        {toolsByTier.required.length > 0 ? (
          <ToolGroup
            title={t("draft_tools_required")}
            tools={toolsByTier.required}
            selected={selectedTools}
            onChange={updateTool}
            t={t}
          />
        ) : null}
        {toolsByTier.recommended.length > 0 ? (
          <ToolGroup
            title={t("draft_tools_recommended")}
            tools={toolsByTier.recommended}
            selected={selectedTools}
            onChange={updateTool}
            t={t}
          />
        ) : null}
        {toolsByTier.risky.length > 0 ? (
          <ToolGroup
            title={t("draft_tools_confirm")}
            tools={toolsByTier.risky}
            selected={selectedTools}
            onChange={updateTool}
            t={t}
          />
        ) : null}
        {draft.tools.length === 0 && selectedTools.size === 0 ? (
          <div style={{ color: "var(--c-ink-3)", fontSize: "0.78125rem" }}>
            {t(draft.warnings.some((warning) => warning.code === "subagent_text_only_confirmation") ? "draft_tools_text_only" : "draft_tools_empty")}
          </div>
        ) : null}
        {(!catalogOpen || catalogError || catalogLoading) && extraSelectedTools.length > 0 ? (
          <div style={{ display: "grid", gap: 6 }}>
            <div style={{ color: "var(--c-ink-3)", fontSize: "0.71875rem", fontWeight: 650 }}>{t("draft_tool_user_selected")}</div>
            {extraSelectedTools.map((tool) => (
              <CatalogToolRow key={tool.stable_ref} tool={tool} locale={locale}
                selected onChange={(checked) => updateCatalogTool(tool, checked)} t={t} />
            ))}
          </div>
        ) : null}
        {unavailableSelectedRefs.map((toolRef) => (
          <label key={toolRef} style={{ display: "flex", gap: 9, padding: 10, border: "1px solid var(--c-warn-line)", borderRadius: 7 }}>
            <input type="checkbox" checked onChange={() => removeUnavailableTool(toolRef)} />
            <span>{t("draft_tool_catalog_unavailable")}</span>
          </label>
        ))}
        <div style={{ display: "grid", gap: 9, marginTop: 4 }}>
          <Button size="sm" onClick={() => {
            if (catalogOpen) setCatalogOpen(false);
            else void openToolCatalog();
          }}>
            {t(catalogOpen ? "draft_tool_catalog_close" : "draft_tool_catalog_open")}
          </Button>
          {catalogOpen ? (
            <div style={{ display: "grid", gap: 9, padding: 12, border: "1px solid var(--c-line)", borderRadius: 8 }}>
              <Field id={`${nameId}-tool-search`} label={t("draft_tool_catalog_search")}>
                <input
                  id={`${nameId}-tool-search`}
                  className="settings-input"
                  type="search"
                  value={toolQuery}
                  onChange={(event) => setToolQuery(event.target.value)}
                />
              </Field>
              {catalogLoading ? <Notice tone="muted">{t("draft_tool_catalog_loading")}</Notice> : null}
              {catalogError ? (
                <Notice tone="error">
                  {t("draft_tool_catalog_error")}
                  <Button size="sm" onClick={() => void openToolCatalog(true)}>{t("draft_reload")}</Button>
                </Notice>
              ) : null}
              {catalogTools && !catalogError && !catalogLoading && otherCatalogTools.length === 0 ? (
                <Notice tone="muted">{t("draft_tool_catalog_empty")}</Notice>
              ) : null}
              {!catalogError && !catalogLoading ? otherCatalogTools.slice(0, 50).map((tool) => (
                <CatalogToolRow
                  key={tool.stable_ref}
                  tool={tool}
                  locale={locale}
                  selected={selectedTools.has(tool.stable_ref)}
                  onChange={(checked) => updateCatalogTool(tool, checked)}
                  t={t}
                />
              )) : null}
              {!catalogError && !catalogLoading && otherCatalogTools.length > 50 ? <p style={{ margin: 0, color: "var(--c-ink-3)", fontSize: "0.75rem" }}>{t("draft_tool_catalog_narrow")}</p> : null}
            </div>
          ) : null}
        </div>
      </Section>

      {pickerLoadError && !catalogPlugins && !catalogSkills ? (
        <Section title={tPicker("plugins")}>
          <div style={{ display: "grid", gap: 6, justifyItems: "start", fontSize: "0.78125rem", color: "var(--c-ink-2)" }}>
            <div role="alert" style={{ display: "grid", gap: 4 }}>
              <strong>{tPicker("catalog_failed_title")}</strong>
              <span>{tPicker("catalog_failed_body")}</span>
            </div>
            <ErrorDiagnosticsInline error={pickerLoadError.error} surface="settings" />
            <Button size="sm" variant="secondary" onClick={() => setPickerReload((value) => value + 1)}>{tPicker("retry")}</Button>
          </div>
        </Section>
      ) : null}
      {catalogPlugins || catalogSkills ? (
        <Section title={tPicker("plugins")}>
          {catalogPlugins ? (
            <ResourcePluginPicker
              plugins={pluginsFromCatalog(catalogPlugins, locale)}
              canManage={Boolean(catalogPlugins.viewer_can_manage)}
              selected={new Set([...resources.builtin_plugin_ids, ...resources.capability_ids])}
              disabled={!draft.actions.edit}
              onToggle={(plugin) => updateResources((current) => {
                const key = plugin.kind === "builtin" ? "builtin_plugin_ids" : "capability_ids";
                const items = current[key];
                return { ...current, [key]: items.includes(plugin.id) ? items.filter((id) => id !== plugin.id) : [...items, plugin.id].sort() };
              })}
            />
          ) : null}
          {catalogSkills ? (
            <ResourceSkillPicker
              skills={catalogSkills}
              selected={new Set(resources.skill_grants.map((grant) => grant.skill_ref.id))}
              disabled={!draft.actions.edit}
              onToggle={(skill) => updateResources((current) => {
                const id = skill.grant.skill_ref.id;
                const kept = current.skill_grants.filter((grant) => grant.skill_ref.id !== id);
                return { ...current, skill_grants: kept.length === current.skill_grants.length ? [...kept, skill.grant] : kept };
              })}
            />
          ) : null}
        </Section>
      ) : null}

      <Section title={t("draft_configuration")}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 12, alignItems: "center" }}>
          <div style={{ fontSize: "0.78125rem", color: "var(--c-ink-2)", lineHeight: 1.55 }}>
            {t("draft_runtime_summary", {
              model: definition.model || "analyst",
            })}
            <br />
            {t("draft_private_note")}
          </div>
          <Button size="sm" onClick={() => setAdvanced((value) => !value)}>
            {advanced ? t("draft_advanced_close") : t("draft_advanced_open")}
          </Button>
        </div>
      </Section>

      {advanced ? (
        <div
          style={{
            display: "grid",
            gap: 13,
            padding: 14,
            borderRadius: 8,
            border: "1px solid var(--c-line)",
            background: "var(--c-surface-2)",
          }}
        >
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
            <Field id={displayNameId} label={t("draft_display_name")}>
              <input
                id={displayNameId}
                className="settings-input"
                maxLength={40}
                value={definition.display_name}
                onChange={(event) => setField("display_name", event.target.value)}
              />
            </Field>
            <Field id={nameId} label={t("draft_slug")}>
              <input
                id={nameId}
                className="settings-input bh-mono"
                value={definition.name}
                onChange={(event) => setField("name", event.target.value)}
              />
            </Field>
          </div>
          <Field id={descriptionId} label={t("draft_description")}>
            <textarea
              id={descriptionId}
              className="settings-input"
              rows={2}
              maxLength={200}
              value={definition.description}
              onChange={(event) => setField("description", event.target.value)}
            />
          </Field>
          <Field id={promptId} label={t("draft_prompt")}>
            <textarea
              id={promptId}
              className="settings-input bh-mono"
              rows={8}
              maxLength={MAX_PROMPT_CHARS_TEAM}
              value={definition.system_prompt}
              onChange={(event) => setField("system_prompt", event.target.value)}
            />
            <PromptBudgetHint text={definition.system_prompt} t={t} />
          </Field>
        </div>
      ) : null}

      <ValidationSummary draft={draft} catalogTools={catalogTools} onResolveToolIssue={() => void openToolCatalog(true)} t={t} />

      {draft.warnings.length > 0 ? (
        <div style={{ display: "grid", gap: 8 }}>
          {draft.warnings.map((warning) => (
            <label
              key={warning.code}
              style={{
                display: "flex",
                gap: 9,
                padding: 10,
                border: "1px solid var(--c-warn-line, var(--c-line))",
                borderRadius: 7,
                background: "var(--c-warn-soft, var(--c-surface-2))",
                color: "var(--c-ink-2)",
                fontSize: "0.78125rem",
                lineHeight: 1.5,
              }}
            >
              <input
                type="checkbox"
                checked={acknowledged.has(warning.code)}
                onChange={(event) => {
                  setAcknowledged((current) => {
                    const next = new Set(current);
                    if (event.target.checked) next.add(warning.code);
                    else next.delete(warning.code);
                    return next;
                  });
                }}
              />
              <span>{warning.message}</span>
            </label>
          ))}
        </div>
      ) : null}

      {error ? (
        <Notice tone="error">
          {error.message}
          {conflict ? (
            <Button
              size="sm"
              style={{ marginLeft: 10 }}
              onClick={() => void load()}
            >
              {t("draft_reload")}
            </Button>
          ) : null}
        </Notice>
      ) : null}
      {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}

      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          flexWrap: "wrap",
          paddingTop: 2,
        }}
      >
        <Button
          variant="ghost"
          disabled={isBusy}
          onClick={() => void discard()}
        >
          {t("draft_discard")}
        </Button>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {onManual ? (
            <Button disabled={isBusy} onClick={() => void moveToManual(manualHint)}>
              {t("manual_create")}
            </Button>
          ) : null}
          {draft.actions.edit ? (
            <Button
              disabled={isBusy || !dirty}
              onClick={() => void save()}
            >
              {busyAction === "saving" ? t("draft_saving") : t("draft_save")}
            </Button>
          ) : null}
          {draft.actions.create ? (
            <Button
              variant="primary"
              disabled={
                isBusy
                || draft.validation.status === "invalid"
                || !allWarningsAcknowledged
              }
              onClick={() => void create()}
            >
              {busyAction === "creating" ? t("draft_creating") : t("draft_create")}
            </Button>
          ) : null}
        </div>
      </div>
    </CardShell>
  );
}

function CardShell({
  title,
  onClose,
  embedded = false,
  busy = false,
  children,
}: {
  title: string;
  onClose: () => void;
  embedded?: boolean;
  busy?: boolean;
  children: React.ReactNode;
}) {
  const t = useTranslations("settings.subagents");
  const titleId = useId();
  const style = {
    border: embedded ? "none" : "1px solid var(--c-line)",
    borderRadius: embedded ? 0 : 12,
    background: "var(--c-surface)",
    boxShadow: embedded ? "none" : "0 18px 60px color-mix(in srgb, var(--c-ink) 16%, transparent)",
    overflow: "hidden",
    color: "var(--c-ink)",
    fontFamily: "var(--font-v2-sans)",
  } as const;
  const content = (
    <>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "15px 18px",
          borderBottom: "1px solid var(--c-line)",
        }}
      >
        <h2 id={titleId} style={{ flex: 1, margin: 0, fontSize: "0.9375rem", fontWeight: 650 }}>
          {title}
        </h2>
        <Button
          variant="ghost"
          size="sm"
          aria-label={t("draft_close")}
          disabled={busy}
          onClick={onClose}
        >
          <CharIcon ch="×" />
        </Button>
      </header>
      <div style={{ display: "grid", gap: 16, padding: 18 }}>
        {children}
      </div>
    </>
  );
  // Embedded in the canvas, this is a nonmodal region; standalone it is a modal with a page-provided backdrop, so omit the shell backdrop.
  if (embedded) {
    return (
      <section role="region" aria-labelledby={titleId} aria-busy={busy || undefined} tabIndex={-1} style={style}>
        {content}
      </section>
    );
  }
  return (
    <DialogShell
      onClose={onClose}
      busy={busy}
      labelledBy={titleId}
      mount="inline"
      overlay="none"
      closeOnBackdrop={false}
      panelStyle={style}
    >
      {content}
    </DialogShell>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section style={{ display: "grid", gap: 9 }}>
      <h3
        style={{
          margin: 0,
          color: "var(--c-ink-3)",
          fontFamily: "var(--font-v2-mono)",
          fontSize: "0.65625rem",
          fontWeight: 650,
          letterSpacing: ".04em",
          textTransform: "uppercase",
        }}
      >
        {title}
      </h3>
      {children}
    </section>
  );
}

function ToolGroup({
  title,
  tools,
  selected,
  onChange,
  t,
}: {
  title: string;
  tools: SubagentDraftTool[];
  selected: Set<string>;
  onChange: (tool: SubagentDraftTool, checked: boolean) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  return (
    <div style={{ display: "grid", gap: 6 }}>
      <div style={{ color: "var(--c-ink-3)", fontSize: "0.71875rem", fontWeight: 650 }}>
        {title}
      </div>
      {tools.map((tool) => (
        <label
          key={tool.tool_ref}
          style={{
            display: "grid",
            gridTemplateColumns: "18px minmax(0, 1fr) auto",
            gap: 9,
            alignItems: "start",
            padding: "9px 10px",
            border: "1px solid var(--c-line)",
            borderRadius: 7,
            background: "var(--c-surface-2)",
            opacity: tool.locked_reason ? 0.62 : 1,
          }}
        >
          <input
            type="checkbox"
            checked={selected.has(tool.tool_ref)}
            disabled={Boolean(tool.locked_reason) && !selected.has(tool.tool_ref)}
            onChange={(event) => onChange(tool, event.target.checked)}
          />
          <span style={{ minWidth: 0 }}>
            <span style={{ display: "block", color: "var(--c-ink)", fontSize: "0.78125rem", fontWeight: 600 }}>
              {tool.display_name}
            </span>
            <span style={{ display: "block", marginTop: 2, color: "var(--c-ink-3)", fontSize: "0.71875rem", lineHeight: 1.45 }}>
              {tool.reason}
              {tool.locked_reason
                ? ` · ${lockedReasonLabel(tool.locked_reason, t)}`
                : ""}
            </span>
          </span>
          <span
            className="bh-mono"
            style={{
              color: isRisky(tool) ? "var(--c-warn, var(--c-ink-3))" : "var(--c-ink-4)",
              fontSize: "0.59375rem",
              whiteSpace: "nowrap",
            }}
          >
            {riskLabel(tool.risk, t)}
          </span>
        </label>
      ))}
    </div>
  );
}

function CatalogToolRow({
  tool,
  locale,
  selected,
  onChange,
  t,
}: {
  tool: AgentAuthoringTool;
  locale: string;
  selected: boolean;
  onChange: (checked: boolean) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  const { name, description } = localizedToolText(tool, locale);
  return (
    <label style={{
      display: "grid",
      gridTemplateColumns: "18px minmax(0, 1fr) auto",
      alignItems: "start",
      gap: 9,
      padding: "9px 10px",
      border: "1px solid var(--c-line)",
      borderRadius: 7,
      background: selected ? "var(--c-accent-tint)" : "var(--c-surface-2)",
    }}>
      <input
        type="checkbox"
        checked={selected}
        disabled={!tool.selectable && !selected}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span style={{ minWidth: 0 }}>
        <span style={{ display: "block", color: "var(--c-ink)", fontSize: "0.78125rem", fontWeight: 600 }}>{name}</span>
        <span style={{ display: "block", marginTop: 2, color: "var(--c-ink-3)", fontSize: "0.71875rem", lineHeight: 1.45 }}>
          {selected ? `${t("draft_tool_user_selected")} · ` : ""}{description}
          {!tool.selectable ? ` · ${lockedReasonLabel(tool.locked_reason || "", t)}` : ""}
        </span>
      </span>
      <span className="bh-mono" style={{ color: "var(--c-ink-4)", fontSize: "0.59375rem", whiteSpace: "nowrap" }}>
        {riskLabel(tool.risk, t)}
      </span>
    </label>
  );
}

function riskLabel(
  risk: string,
  t: ReturnType<typeof useTranslations>,
): string {
  switch (risk) {
    case "read":
      return t("draft_risk_read");
    case "compute":
      return t("draft_risk_compute");
    case "artifact_write":
      return t("draft_risk_artifact_write");
    case "external_write":
      return t("draft_risk_external_write");
    case "destructive":
      return t("draft_risk_destructive");
    default:
      return t("draft_risk_unknown");
  }
}

function lockedReasonLabel(
  reason: string,
  t: ReturnType<typeof useTranslations>,
): string {
  return t(toolSelectionLockedReasonKey(reason));
}

function ValidationSummary({
  draft,
  catalogTools,
  onResolveToolIssue,
  t,
}: {
  draft: SubagentDraftDetail;
  catalogTools: AgentAuthoringTool[] | null;
  onResolveToolIssue: () => void;
  t: ReturnType<typeof useTranslations>;
}) {
  const tMarket = useTranslations("agents_marketplace");
  const valid = draft.validation.status === "valid";
  const warning = draft.validation.status === "warning";
  const hasToolIssue = draft.validation.issues.some((issue) => issue.field === "tool_refs");
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        border: `1px solid ${
          valid
            ? "var(--c-accent-line)"
            : warning
              ? "var(--c-warn-line, var(--c-line))"
              : "var(--c-err, var(--c-neg))"
        }`,
        borderRadius: 7,
        padding: "10px 12px",
        background: valid
          ? "var(--c-accent-tint)"
          : warning
            ? "var(--c-warn-soft, var(--c-surface-2))"
            : "var(--c-err-soft, var(--c-neg-soft))",
        color: "var(--c-ink-2)",
        fontSize: "0.78125rem",
        lineHeight: 1.5,
      }}
    >
      <strong>
        {valid
          ? t("draft_validation_valid")
          : warning
            ? t("draft_validation_warning")
            : t("draft_validation_invalid")}
      </strong>
      {draft.validation.issues.length > 0 ? (
        <ul style={{ margin: "5px 0 0", paddingLeft: 18 }}>
          {draft.validation.issues.map((issue, index) => {
            const toolName = draft.tools.find((tool) => tool.tool_ref === issue.tool_ref)?.display_name
              || catalogTools?.find((tool) => tool.stable_ref === issue.tool_ref)?.display_name
              || t("draft_tool_unknown");
            const named = dependencyIssueItems(issue.details);
            const message = issue.message
              || (named.length ? blockedItemLines(named, (key, values) => tMarket(key as never, values as never), (item) => (
                item.kind === "tool"
                  ? draft.tools.find((tool) => tool.tool_ref === item.identity)?.display_name
                    || catalogTools?.find((tool) => tool.stable_ref === item.identity)?.display_name
                  : undefined
              )).join(" ") : null)
              || (issue.code === "subagent_tool_required"
                ? t("draft_validation_tool_required")
                : issue.code === "subagent_tool_no_longer_available" || issue.code === "subagent_tool_not_selectable"
                  ? t("draft_validation_tool_unavailable", { name: toolName })
                  : t("draft_validation_unknown"));
            return <li key={`${issue.code}:${index}`}>{message}</li>;
          })}
        </ul>
      ) : null}
      {hasToolIssue ? <Button size="sm" style={{ marginTop: 8 }} onClick={onResolveToolIssue}>{t("draft_tool_catalog_resolve")}</Button> : null}
    </div>
  );
}

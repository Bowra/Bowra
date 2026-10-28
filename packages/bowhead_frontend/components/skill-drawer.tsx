"use client";

/**
 * SkillDrawer — /skills right-side detail drawer (IA redesign, 2026-07-10).
 *
 * Replaces centered SkillPreviewModal. Visuals verbatim from Claude Design handoff:
 * header (title + switch + close; slug·source·version·size·creation time) → "使用的工具" chips
 * (⚠ declaration only, not enforced; never label as "允许") → reverse "被子代理使用" rows →
 * 4 tabs (Markdown description / trigger examples / files [references+scripts, click for nested content view] / evaluation).
 * Team-shared skills use the same tabs (GET /team-skills/{id}, files via .../files/{kind}/{f});
 * team data has no trigger_examples field, so that tab is naturally absent.
 * Must render within .sset scope (styles/skills-settings.css).
 */

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import {
  changeSkillFile,
  type SkillFileChange,
  fetchSkillDetail,
  overrideSkillImport,
  fetchSkillFile,
  fetchSkillUsage,
  putSkillBody,
  putSkillFile,
  type SkillDetailDTO,
  type SkillFileContentDTO,
  type SkillSummaryDTO,
} from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import type { ErrorPresentation } from "@/lib/error-presentation";
import { skillWorkflowErrorPresentation } from "@/lib/skill-workflow-error";
import {
  changeTeamSkillFile,
  copyTeamSkillToPersonal,
  getTeamSkill,
  getTeamSkillFile,
  getTeamSkillUsage,
  promoteSkill,
  putTeamSkillBody,
  putTeamSkillFile,
  withdrawTeamSkill,
  type TeamSkillDetail,
  type TeamSkillPublic,
} from "@/lib/workflow-solidify-api";
import { teamSkillOwnerName } from "@/lib/team-skill-owner";
import { deriveTeamSyncStatus, type TeamSyncStatus } from "@/lib/team-skill-approval";
import { skillTitle } from "@/lib/skills-meta";
import { useAppRouter } from "@/lib/use-app-router";
import { formatByteSize, formatDateYMD } from "@/lib/format";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { SaveBar } from "@/components/ui/save-bar";
import { CanvasToolbarActions, canvasCloseGuardOptions } from "@/components/canvas/object";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import { DialogShell } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { Tab as UiTab, TabList, Tabs } from "@/components/ui/tabs";
import { ResourceUsageDetails } from "@/components/settings/resource-usage";
import { FeedbackPrefillPanel } from "@/components/agents/feedback-prefill-panel";
import { ErrorNotice } from "@/components/error-notice";
import { SkillImportFindings, skillImportSummary } from "@/components/skill-import-findings";
import { SkillCoachDraftPanel } from "@/components/skill-coach-draft-panel";
import { requestSkillCoachDraft, type SkillCoachDraft } from "@/lib/skill-coach-api";
import { ArrowLeft, ChevronRight, FileCode, FileText, Trash, X } from "@/components/icons";
import { Markdown } from "@/lib/md";
import { clearFeedbackPrefill, type FeedbackPrefillHandoff } from "@/lib/feedback-candidates-api";
import type { ResourceUsageResponse } from "@/lib/resource-usage";
import type { CoachEditorPort, CoachEditorSnapshot } from "@/lib/coach-canvas-target";
import type { CoachResourceReference } from "@/lib/coach-resource-editors";
import { CharIconText } from "@/components/char-icon";

/* Drawer target: built-in/mine (skill name) or space-shared (team skill row). */
export type SkillDrawerTarget =
  | { kind: "skill"; skill: SkillSummaryDTO; initialTab?: "usage" }
  /** canEdit: may edit team skill content (manageable ∧ [not broadcast ∨ admin], calculated by parent from roles). */
  | {
      kind: "team";
      skill: TeamSkillPublic;
      canManage: boolean;
      canEdit: boolean;
      initialTab?: "usage";
      /** agent-feedback-loop FE batch C: skill-body draft handed off by inbox "采纳并预填" (passed after page-level peek). */
      prefill?: FeedbackPrefillHandoff | null;
    };

type Tab = "desc" | "usage" | "trig" | "files" | "evals";

export type SkillEditorPort = CoachEditorPort;

interface Props {
  hosted?: boolean;
  readOnly?: boolean;
  controllerRef?: React.RefObject<SkillEditorPort | null>;
  chatReference?: CoachResourceReference;
  onActivate?: () => void;
  target: SkillDrawerTarget;
  onClose: () => void;
  /** Built-in/mine: per-user enable/disable (parent updates after server confirmation). */
  onToggle?: (name: string, enabled: boolean) => void;
  /** Team-shared: per-user enable/disable (self only; team governance on workflows page). */
  onTeamPerUserToggle?: (s: TeamSkillPublic, enabled: boolean) => void;
  /** My skill: delete (parent confirmation dialog). */
  onDelete?: (skill: SkillSummaryDTO) => void;
  // ── skill-team-governance P3（PRD §3.6 B/D）──
  /** Current user ID (team status distinguishes same name owned by someone else). */
  meId?: string | null;
  /** Same-name team row (author sees approval/candidate details; absent → unsubmitted). */
  teamRow?: TeamSkillPublic | null;
  /** Callback after promote/withdraw/copy persists (parent refreshes personal + team lists). */
  onTeamStateChanged?: () => void;
  /** "用对话修改" CTA (parent prefills composer and navigates). */
  onEditInChat?: (name: string) => void;
  /** Host may dismiss modal before leaving settings; standalone pages default to app router. */
  onNavigateAway?: (href: string) => void;
  /** skill-import batch 4: /me feedback_loop capability — off hides "让教练改";
   * server gate also returns 404, using the same bit on both sides; rendering an unusable action falsely promises execution. */
  coachEnabled?: boolean;
  /** workflow_creator capability (/me capabilities) — off hides the entire team-status region
   * (review P2: otherwise flag-off shows an always-404 "提交到团队" button, violating zero behavior change). */
  workflowCreatorEnabled?: boolean;
}

function SkillErrorNotice({ error }: { error: ErrorPresentation }) {
  return <ErrorNotice message={error.message} diagnostics={error.diagnostics} surface="settings" diagnosticsEnabled />;
}

export function SkillDrawer(props: Props) {
  const { target, meId } = props;
  const key = JSON.stringify([meId, target.kind, target.skill.id, target.skill.name,
    target.kind === "skill" ? target.skill.source_type : null]);
  return <SkillDrawerInstance key={key} {...props} />;
}

function SkillDrawerInstance({
  target,
  hosted = false,
  readOnly = false,
  controllerRef,
  chatReference,
  onActivate,
  onClose,
  onToggle,
  onTeamPerUserToggle,
  onDelete,
  meId,
  teamRow,
  onTeamStateChanged,
  onEditInChat,
  onNavigateAway,
  coachEnabled,
  workflowCreatorEnabled,
}: Props) {
  const originalChatReference = useRef(chatReference);
  const captureEditor = useRef<() => CoachEditorSnapshot | null>(() => null);
  const tabIdentity = useId();
  const tabsId = hosted ? `skill-editor-${tabIdentity}` : "skill-drawer";
  const mounted = useRef(true);
  const pendingActions = useRef(false);
  const approvedGeneration = useRef<number | null>(null);
  const savingRef = useRef(false);
  const leaveIntent = useRef(0);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; leaveIntent.current += 1; };
  }, []);
  const t = useTranslations("skills_page");
  const tObject = useTranslations("canvas_object");
  const ti = useTranslations("skill_import");
  const tc = useTranslations("skill_coach");
  const locale = useLocale();
  const te = useTranslations("errors");
  const ownerFallback = useTranslations("agents_marketplace")("team_member");
  const presentError = useCallback(
    (error: unknown) =>
      skillWorkflowErrorPresentation(error, {
        surface: "settings",
        copy: {
          generic: t("error_generic"),
          transport: {
            requestTimedOut: te("transport.requestTimedOut"),
            serviceUnreachable: te("transport.serviceUnreachable"),
            retry: te("transport.retry"),
          },
        },
      }),
    [t, te],
  );
  const router = useAppRouter();
  const [tab, setTab] = useState<Tab>("desc");
  /** Hosted (D10): the frontmatter kept aside while only the instructions are edited; re-attached on save. */
  const hostedFront = useRef<string | null>(null);
  const [detail, setDetail] = useState<SkillDetailDTO | null>(null);
  const [teamDetail, setTeamDetail] = useState<TeamSkillDetail | null>(null);
  const [err, setErr] = useState<ErrorPresentation | null>(null);
  const [fileView, setFileView] = useState<SkillFileContentDTO | null>(null);
  const [fileErr, setFileErr] = useState<ErrorPresentation | null>(null);
  const [evalsContent, setEvalsContent] = useState<string | null>(null);
  const [evalsErr, setEvalsErr] = useState<ErrorPresentation | null>(null);
  const [usage, setUsage] = useState<ResourceUsageResponse | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageErr, setUsageErr] = useState<string | null>(null);
  // Editing state (see Direct Editing in Settings below).
  const [editingPath, setEditingPath] = useState<string | null>(null);
  const editing = editingPath !== null;
  const [initialDraft, setInitialDraft] = useState("");
  const [editRevision, setEditRevision] = useState<string | undefined>();
  const fileRequest = useRef(0);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<ErrorPresentation | null>(null);
  const [savedAt, setSavedAt] = useState(0);
  const [creating, setCreating] = useState(false);
  const [newKind, setNewKind] = useState<SkillFileChange["kind"]>("references");
  const [newFilename, setNewFilename] = useState("");
  const [renameTarget, setRenameTarget] = useState<string | null>(null);
  const editState = { editingPath, initialDraft, draft, creating, renameTarget, newKind, newFilename, editRevision };
  const latestEdit = useRef(editState);
  const editGeneration = useRef(0);
  if ((Object.keys(editState) as Array<keyof typeof editState>).some((key) => latestEdit.current[key] !== editState[key])) {
    editGeneration.current += 1;
    latestEdit.current = editState;
  }

  const isTeam = target.kind === "team";
  const skillName = target.skill.name;
  const loadPersonalDetail = useCallback(async () => {
    const value = await fetchSkillDetail(skillName);
    if (hosted && target.kind === "skill" && target.skill.id && value.id !== target.skill.id)
      throw new Error("The original Skill is no longer available");
    return value;
  }, [skillName, hosted, target.kind, target.skill.id]);

  const teamId = target.kind === "team" ? target.skill.id : "";
  const usageAvailable = target.skill.usage_summary !== undefined;
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  const leaveEditor = useCallback(
    async (action: () => void, preserveBuffer = false) => {
      const intent = ++leaveIntent.current;
      const generation = editGeneration.current;
      if (!mounted.current || savingRef.current || pendingActions.current) return false;
      if ((editing && draft !== initialDraft) || creating || renameTarget !== null) {
        const ok = await confirm(hosted
          ? canvasCloseGuardOptions(tObject, target.skill.name)
          : {
            title: t("drawer.discard_title"),
            description: t("drawer.discard_confirm"),
            confirmLabel: t("drawer.discard_action"),
            cancelLabel: t("drawer.discard_keep"),
          });
        if (!ok) return false;
      }
      if (!mounted.current || savingRef.current || pendingActions.current || leaveIntent.current !== intent || editGeneration.current !== generation) return false;
      approvedGeneration.current = generation;
      // Another editor can still cancel whole-page navigation. Keep this input
      // until actual disposal; local close and tab changes may reset it now.
      if (preserveBuffer) return true;
      fileRequest.current += 1;
      setEditingPath(null);
      if (creating) setFileView(null);
      setCreating(false);
      setRenameTarget(null);
      setSaveErr(null);
      setSavedAt(0);
      action();
      return true;
    },
    [editing, draft, initialDraft, creating, renameTarget, t, tObject, hosted, target.skill.name, confirm],
  );
  const close = useCallback(() => void leaveEditor(onClose), [leaveEditor, onClose]);
  const host = useRef({ leaveEditor, close, onActivate });
  host.current = { leaveEditor, close, onActivate };
  useEffect(() => {
    if (!hosted) return;
    const port: SkillEditorPort = { requestClose: (afterClose) => afterClose ? void host.current.leaveEditor(afterClose) : host.current.close(), capture: () => captureEditor.current() };
    if (controllerRef) controllerRef.current = port;
    const hasUnsaved = () => {
        const edit = latestEdit.current;
        return savingRef.current || pendingActions.current || (approvedGeneration.current !== editGeneration.current
          && (edit.creating || edit.renameTarget !== null || edit.editingPath !== null && edit.draft !== edit.initialDraft));
    };
    const unregister = registerSubagentDraftNavigationGuard({
      hasUnsaved,
      confirmLeave: async () => {
        if (!hasUnsaved()) return true;
        host.current.onActivate?.();
        return host.current.leaveEditor(() => {}, true);
      },
      cancelLeave: () => { leaveIntent.current += 1; approvedGeneration.current = null; },
    });
    return () => { unregister(); if (controllerRef?.current === port) controllerRef.current = null; };
  }, [hosted, controllerRef]);

  useEffect(() => {
    if (!saving && !creating && renameTarget === null && (!editing || draft === initialDraft)) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [editing, draft, initialDraft, creating, renameTarget, saving]);

  const loadUsage = useCallback(async () => {
    setUsageLoading(true);
    setUsageErr(null);
    try {
      setUsage(await (isTeam ? getTeamSkillUsage(teamId) : fetchSkillUsage(skillName)));
    } catch (error) {
      setUsageErr(presentError(error).message);
    } finally {
      setUsageLoading(false);
    }
  }, [isTeam, presentError, skillName, teamId]);

  // ── skill-team-governance P3: team status (personal skills) / Copy to Mine (team skills) ──
  const [teamBusy, setTeamBusy] = useState(false);
  const [teamErr, setTeamErr] = useState<ErrorPresentation | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "confirm" | "done">("idle");
  const runTeamAction = useCallback(
    async (fn: () => Promise<unknown>) => {
      setTeamBusy(true);
      setTeamErr(null);
      try {
        await fn();
        onTeamStateChanged?.();
      } catch (e) {
        setTeamErr(presentError(e));
        if (e instanceof ApiError && e.code === "skill_promote_content_stale") {
          // The saved Skill changed elsewhere (chat, Coach, another tab). Reload it so the next submit sends the
          // current content instead of repeating the stale snapshot the error asks the user to refresh.
          try {
            setDetail(await loadPersonalDetail());
            onTeamStateChanged?.();
          } catch {
            // Keep the original error; the drawer's normal load path reports read failures.
          }
        }
      } finally {
        setTeamBusy(false);
      }
    },
    [onTeamStateChanged, presentError, loadPersonalDetail],
  );
  const doCopy = useCallback(
    async (overwrite: boolean) => {
      setTeamBusy(true);
      setTeamErr(null);
      try {
        await copyTeamSkillToPersonal(teamId, overwrite);
        setCopyState("done");
        onTeamStateChanged?.();
      } catch (e) {
        if (e instanceof ApiError && e.code === "E_SKILL_NAME_EXISTS") {
          setCopyState("confirm"); // Same-name personal skill exists → overwrite confirmation (PRD §3.4).
        } else {
          setTeamErr(presentError(e));
        }
      } finally {
        setTeamBusy(false);
      }
    },
    [teamId, onTeamStateChanged, presentError],
  );

  // Depend on skill identity key, not target reference: parent toggle replaces the target object
  // to synchronize switch state, but the same skill must not refetch details / reset tabs.
  useEffect(() => {
    let cancelled = false;
    fileRequest.current += 1;
    setDetail(null);
    setTeamDetail(null);
    setErr(null);
    setTab(target.initialTab === "usage" && usageAvailable ? "usage" : "desc");
    setFileView(null);
    setEvalsContent(null);
    setEvalsErr(null);
    setUsage(null);
    setUsageErr(null);
    setUsageLoading(usageAvailable);
    setEditingPath(null);
    setSaveErr(null);
    setSavedAt(0);
    // Coach state likewise: when reusing the drawer for another skill, discard edits based on the previous skill's body.
    setCoachDraft(null);
    setCoachErr(null);
    // Team-status / copy-flow state also resets by skill identity (review P3: future skill switches without unmounting
    // must not leak the previous skill's copy-confirm / error copy).
    setTeamBusy(false);
    setTeamErr(null);
    setCopyState("idle");
    (async () => {
      try {
        if (!isTeam) {
          const d = await loadPersonalDetail();
          if (!cancelled) setDetail(d);
        } else {
          const d = await getTeamSkill(teamId);
          if (!cancelled) setTeamDetail(d);
        }
      } catch (e) {
        if (!cancelled) {
          setErr(presentError(e));
        }
      }
    })();
    if (usageAvailable) void loadUsage();
    return () => {
      cancelled = true;
      fileRequest.current += 1;
    };
  }, [isTeam, skillName, teamId, presentError, target.initialTab, usageAvailable, loadUsage, loadPersonalDetail]);

  // Personal files use skill name; team files use row ID. Both return SkillFileContentDTO.
  const loadFile = useCallback(
    (kind: "references" | "scripts" | "evals", filename: string) =>
      isTeam ? getTeamSkillFile(teamId, kind, filename) : fetchSkillFile(skillName, kind, filename),
    [isTeam, teamId, skillName],
  );

  const openFile = useCallback(
    async (kind: "references" | "scripts" | "evals", filename: string) => {
      const requestId = ++fileRequest.current;
      setFileErr(null);
      setEditingPath(null);
      setSaveErr(null);
      setSavedAt(0);
      try {
        const file = await loadFile(kind, filename);
        if (requestId === fileRequest.current) setFileView(file);
      } catch (e) {
        if (requestId === fileRequest.current) setFileErr(presentError(e));
      }
    },
    [loadFile, presentError],
  );

  const hasEvals = (isTeam ? teamDetail?.has_evals : detail?.has_evals) ?? false;
  useEffect(() => {
    if (tab !== "evals" || evalsContent !== null || !hasEvals) return;
    let cancelled = false;
    setEvalsErr(null);
    (async () => {
      try {
        const f = await loadFile("evals", "evals.json");
        if (!cancelled) setEvalsContent(f.content);
      } catch (e) {
        if (!cancelled) {
          setEvalsErr(presentError(e));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tab, evalsContent, hasEvals, loadFile, presentError]);

  /* ── Editing (settings and Agent saves share target-write permissions; draft always bound to file path) ──
     Parent determines editability and passes target: personal=not built-in; team=manageable ∧ (not broadcast ∨ admin),
     same rule as backend team_skill_content_admin_required. FE controls entry visibility; server enforces the gate. */
  const canEdit = !readOnly && (target.kind === "skill" ? target.skill.source_type !== "builtin" : target.canEdit);
  const detailRevision = (isTeam ? teamDetail : detail)?.content_sha256 ?? undefined;
  const beginEdit = useCallback((path: string, initial: string, revision?: string) => {
    setEditRevision(revision);
    setInitialDraft(initial);
    setDraft(initial);
    setSaveErr(null);
    setEditingPath(path);
  }, []);
  /* ── skill-import batch 3: pending import status (personal skills) ──
     Red=ineffective (excluded from chat catalog and Agent selection). Two paths: Edit (save hook reruns inventory),
     Enable Anyway (overridden, knowingly using red results). Add "让教练改" after B-track personal-skill endpoints ship. */
  const [overriding, setOverriding] = useState(false);
  // Batch 4: coach edits. No draft object persisted; compare response on this page, apply into editor draft, save through existing body API.
  const [coachDraft, setCoachDraft] = useState<SkillCoachDraft | null>(null);
  const [coachBusy, setCoachBusy] = useState(false);
  // Separate error state: this status-region action renders errors below its button. Reusing saveErr moves them past the full manifest / tab bar,
  // leaving users with '按钮闪了一下，没事发生' (review P2-7).
  const [coachErr, setCoachErr] = useState<ErrorPresentation | null>(null);
  pendingActions.current = teamBusy || overriding || coachBusy;
  captureEditor.current = () => {
    const reference = originalChatReference.current;
    const value = isTeam ? teamDetail : detail;
    if (!mounted.current || !reference || reference.kind !== "skill" || !reference.source_kind || !value) return null;
    const revision = editing ? editRevision : fileView?.content_sha256 ?? value.content_sha256;
    if (reference.source_kind !== "builtin" && !revision) return null;
    return { target: { kind: "skill", id: reference.id, source_kind: reference.source_kind,
      revision: reference.source_kind === "builtin" ? reference.revision : `sha256:${revision}` },
      dirty: creating || renameTarget !== null || editing && draft !== initialDraft,
      pending_write: savingRef.current || pendingActions.current };
  };
  const askCoach = useCallback(async () => {
    setCoachBusy(true);
    setCoachErr(null);
    try {
      setCoachDraft(await requestSkillCoachDraft(skillName));
    } catch (e) {
      // 429 / 409 / 404 mean different things: return tomorrow, configure models, or this path is currently unavailable.
      // Calling all of them "稍后重试" would consume the remaining daily limit waiting for recovery that never comes.
      if (e instanceof ApiError && (e.status === 429 || e.status === 409 || e.status === 404)) {
        setCoachErr({ message: e.status === 429 ? tc("throttled") : e.status === 409 ? tc("unavailable") : tc("gone") });
        if (e.status === 404) {
          // It may no longer be editable; refetch instead of retaining an unusable button.
          try {
            setDetail(await fetchSkillDetail(skillName));
          } catch {
            /* Keep current state if refetch fails. */
          }
        }
      } else {
        setCoachErr(presentError(e));
      }
    } finally {
      setCoachBusy(false);
    }
  }, [skillName, tc, presentError]);
  const importStatus = isTeam ? null : (detail?.import_status ?? target.skill.import_status ?? null);
  const doOverride = useCallback(async () => {
    const ok = await confirm({
      title: ti("override_confirm_title"),
      description: ti("override_confirm"),
      confirmLabel: ti("override"),
    });
    if (!ok) return;
    setOverriding(true);
    try {
      await overrideSkillImport(skillName);
      setDetail(await fetchSkillDetail(skillName));
      onTeamStateChanged?.(); // Parent refreshes the list so badges follow changes.
    } catch (e) {
      setSaveErr(presentError(e));
      // 409 means server no longer considers it pending (changed elsewhere). Without refetch, this region retains a button
      // that always returns 409 (review P2-3).
      if (e instanceof ApiError && e.status === 409) {
        try {
          setDetail(await fetchSkillDetail(skillName));
          // Successful refetch changes this region's message; retaining '无需仍然启用' above contradicts it (re-review P2-6).
          setSaveErr(null);
          onTeamStateChanged?.();
        } catch {
          /* Keep current state if refetch fails; error is already visible. */
        }
      }
    } finally {
      setOverriding(false);
    }
  }, [skillName, ti, onTeamStateChanged, presentError, confirm]);
  // Prefill enters editor draft only (initialDraft remains current body ⇒ immediately dirty); save retains putTeamSkillBody's original gate.
  const [feedbackPrefill, setFeedbackPrefill] = useState<FeedbackPrefillHandoff | null>(null);
  const targetPrefill = target.kind === "team" ? (target.prefill ?? null) : null;
  useEffect(() => {
    // Follow target: reusing the drawer for another skill must discard the old handoff.
    // Dependency is object identity: parent target updates must keep prefill reference stable (spread cur in functional update),
    // otherwise applied/discarded handoffs reappear in the panel.
    setFeedbackPrefill(targetPrefill);
  }, [targetPrefill, isTeam, teamId, skillName]);
  const applyFeedbackPrefill = useCallback((edited: string, current: string) => {
    setEditRevision(current === (isTeam ? teamDetail : detail)?.body_raw ? detailRevision : undefined);
    setInitialDraft(current);
    setDraft(edited);
    setSaveErr(null);
    setEditingPath("SKILL.md");
    setFeedbackPrefill(null);
    clearFeedbackPrefill();
  }, [detail, detailRevision, isTeam, teamDetail]);
  const cancelEdit = useCallback(() => {
    void leaveEditor(() => {});
  }, [leaveEditor]);
  const runSave = useCallback(
    async (save: () => Promise<SkillFileContentDTO>, apply: (saved: SkillFileContentDTO) => void | Promise<void>) => {
      if (!mounted.current || savingRef.current || readOnly) return;
      savingRef.current = true;
      setSaving(true);
      setSaveErr(null);
      try {
        const saved = await save();
        if (!mounted.current) return;
        await apply(saved);
        if (!mounted.current) return;
        setEditingPath(null);
        setCreating(false);
        setRenameTarget(null);
        setSavedAt(Date.now());
        onTeamStateChanged?.(); // Body changes update description / size; parent must refetch list.
      } catch (e) {
        // Preserve the buffer and baseline; an interrupted write or readback may follow a successful save.
        if (mounted.current) setSaveErr(presentError(e));
      } finally {
        savingRef.current = false;
        if (mounted.current) setSaving(false);
      }
    },
    [onTeamStateChanged, presentError, readOnly],
  );
  const changeFile = useCallback(
    (change: SkillFileChange) => (isTeam ? changeTeamSkillFile(teamId, change) : changeSkillFile(skillName, { ...change, ...(target.skill.id ? { expected_instance_id: target.skill.id } : {}) })),
    [isTeam, teamId, skillName, target.skill.id],
  );
  const refreshDetail = useCallback(async () => {
    if (isTeam) setTeamDetail(await getTeamSkill(teamId));
    else setDetail(await loadPersonalDetail());
    setEvalsContent(null);
  }, [isTeam, teamId, skillName, loadPersonalDetail]);
  const manageFile = async (operation: "rename" | "delete") => {
    if (!fileView) return;
    if (!fileView.content_sha256) {
      setSaveErr({ message: t("drawer.version_unavailable") });
      return;
    }
    if (operation === "delete") {
      const ok = await confirm({
        title: t("drawer.delete_file_title", { path: fileView.path }),
        description: t("drawer.delete_file_confirm"),
        confirmLabel: t("drawer.delete_file"),
      });
      if (!ok) return;
    }
    const [kind, ...parts] = fileView.path.split("/");
    void runSave(
      () =>
        changeFile({
          operation,
          kind: kind as SkillFileChange["kind"],
          filename: parts.join("/"),
          target: renameTarget ?? undefined,
          expected_content_sha256: fileView.content_sha256 ?? undefined,
        }),
      async (saved) => {
        setFileView(operation === "delete" ? null : saved);
        await refreshDetail();
      },
    );
  };
  const saveFile = useCallback(() => {
    if (!editingPath || editingPath === "SKILL.md") return;
    if (!editRevision) {
      setSaveErr({ message: t("drawer.version_unavailable") });
      return;
    }
    const slash = editingPath.indexOf("/");
    const kind = editingPath.slice(0, slash) as "references" | "scripts" | "evals";
    const filename = editingPath.slice(slash + 1);
    return runSave(
      () =>
        creating
          ? changeFile({ operation: "create", kind, filename, content: draft, expected_content_sha256: editRevision })
          : isTeam
            ? putTeamSkillFile(teamId, kind, filename, draft, editRevision)
            : putSkillFile(skillName, kind, filename, draft, editRevision, ...(target.skill.id ? [target.skill.id] : [])),
      async (saved) => {
        setFileView(saved);
        if (isTeam) setTeamDetail(await getTeamSkill(teamId));
        else setDetail(await loadPersonalDetail());
        if (kind === "evals" && filename === "evals.json") setEvalsContent(saved.content);
      },
    );
  }, [editingPath, isTeam, teamId, skillName, draft, runSave, creating, changeFile, editRevision, t, target.skill.id, loadPersonalDetail]);
  const saveBody = useCallback(
    () => {
      if (!editRevision) {
        setSaveErr({ message: t("drawer.version_unavailable") });
        return;
      }
      return editingPath === "SKILL.md"
        ? runSave(
            () => {
              // Hosted editing holds the instructions only; a full manifest (e.g. a Coach draft) is saved as is.
              const content = hosted && hostedFront.current && !draft.startsWith("---") ? hostedFront.current + draft : draft;
              return isTeam ? putTeamSkillBody(teamId, content, editRevision) : putSkillBody(skillName, content, editRevision, ...(target.skill.id ? [target.skill.id] : []));
            },
            // Body changes affect displayed body / description / size / fingerprint; refetch details instead of assembling fields locally.
            async () => {
              if (isTeam) setTeamDetail(await getTeamSkill(teamId));
              else setDetail(await loadPersonalDetail());
            },
          )
        : undefined;
    },
    [editingPath, isTeam, teamId, skillName, draft, runSave, editRevision, t, target.skill.id, loadPersonalDetail, hosted],
  );

  const editBar = (onSave: () => void) => hosted ? (
    <>
      <SaveBar
        dirty={creating || renameTarget !== null || draft !== initialDraft}
        saving={saving}
        failed={saveErr !== null}
        saveDisabled={readOnly}
        guard={false}
        onSave={onSave}
        onDiscard={cancelEdit}
      />
      {saveErr ? <SkillErrorNotice error={saveErr} /> : null}
    </>
  ) : (
    <div className="fileview-actions">
      <button type="button" className="btn sec sm" disabled={saving || readOnly} onClick={onSave}>
        {saving ? t("drawer.saving") : t("drawer.save")}
      </button>
      <button type="button" className="btn ghost sm" disabled={saving} onClick={cancelEdit}>
        {t("cancel")}
      </button>
      {saveErr ? <SkillErrorNotice error={saveErr} /> : null}
    </div>
  );

  /* ── Header data ── */
  const title = target.kind === "skill" ? skillTitle(detail ?? target.skill) : (teamDetail ?? target.skill).display_name || target.skill.name;
  const enabled = target.kind === "skill" ? target.skill.enabled : target.skill.status === "enabled";
  const sizeBytes = (isTeam ? teamDetail?.size_bytes : detail?.size_bytes) ?? target.skill.size_bytes;
  const createdAt = target.kind === "skill" ? target.skill.created_at : (target.skill.created_at ?? null);
  const sourceLabel = target.kind === "team" ? t("group.shared") : t(`source.${target.skill.source_type}` as Parameters<typeof t>[0]);
  const deletable = target.kind === "skill" && target.skill.source_type !== "builtin" && !!onDelete;

  /* Both sources share detail UI (design brief §4.5 drawer is generic; team's previous description-only tab
     resulted from backend omitting bundle files, not a design tradeoff). Team data has no trigger_examples field. */
  const loaded = isTeam ? teamDetail !== null : detail !== null;
  const bodyMd = (isTeam ? teamDetail?.body_md : detail?.body_md) ?? "";
  /* Editor draft must be **complete SKILL.md** (including frontmatter); body_md is display text with frontmatter stripped,
     so saving it produces a manifest without name/description and server rejects (reproduced on real 116).
     Legacy backends without body_raw fall back to body_md: prefer readable validation errors to silent corruption. */
  const bodyRaw = (isTeam ? teamDetail?.body_raw : detail?.body_raw) || bodyMd;
  const references = (isTeam ? teamDetail?.references : detail?.references) ?? [];
  const scriptFiles = (isTeam ? teamDetail?.scripts : detail?.scripts) ?? [];
  const evalsCount = (isTeam ? teamDetail?.evals_count : detail?.evals_count) ?? 0;
  // D10 · hosted in Canvas the Skill is directly editable: the body opens in the editor and the shared
  // save bar appears once it changes (no separate “Edit → Save / Cancel” step).
  // Users edit the instructions only; the manifest header (name, description) is never shown or exposed.
  useEffect(() => {
    if (!hosted || !canEdit || !loaded || editing || fileView || tab !== "desc" || coachDraft || feedbackPrefill) return;
    const { front, body: instructions } = splitSkillManifest(bodyRaw);
    hostedFront.current = front;
    beginEdit("SKILL.md", instructions, detailRevision);
  }, [hosted, canEdit, loaded, editing, fileView, tab, coachDraft, feedbackPrefill, bodyRaw, detailRevision, beginEdit]);
  const triggerExamples = (isTeam ? undefined : detail?.trigger_examples) ?? [];
  const authoringEnabled = canEdit && !!(isTeam ? teamDetail?.authoring_enabled : detail?.authoring_enabled);
  const evalFiles = (isTeam ? teamDetail?.eval_files : detail?.eval_files) ?? [];
  const fileCount = references.length + scriptFiles.length + evalFiles.length;
  const tabs: { id: Tab; label: string }[] = [];
  if (loaded) {
    tabs.push({ id: "desc", label: t("drawer.tab_desc") });
    if (usageAvailable && !hosted) tabs.push({ id: "usage", label: t("drawer.tab_usage") });
    // Hosted in Canvas the segments are fixed (D10): instructions / trigger examples / files / evaluations.
    if (triggerExamples.length || hosted) tabs.push({ id: "trig", label: t("drawer.tab_trig") });
    if (fileCount > 0 || authoringEnabled || hosted)
      tabs.push({
        id: "files",
        label: t("drawer.tab_files", { count: fileCount }),
      });
    if (hasEvals || hosted) tabs.push({ id: "evals", label: t("drawer.tab_evals") });
  }

  /* ── Switch region (same semantics as inline) ── */
  let headSwitch: ReactNode = null;
  if (target.kind === "skill" && onToggle) {
    headSwitch = (
      <Switch
        className={`sw${enabled ? " on" : ""}`}
        checked={enabled}
        aria-label={t("toggle_aria", { name: title })}
        title={t("scope_user_tooltip")}
        onCheckedChange={(next) => onToggle(target.skill.name, next)}
      >
        <span className="knob" />
      </Switch>
    );
  } else if (target.kind === "team" && (target.skill.approval || "approved") === "approved" && onTeamPerUserToggle) {
    // Per-user switch (self only); team enable/disable at /team/settings/workflows (admin governance).
    const perUserOn = target.skill.my_enabled !== false;
    headSwitch = (
      <Switch
        className={`sw${perUserOn ? " on" : ""}`}
        checked={perUserOn}
        aria-label={t("toggle_aria", { name: title })}
        title={t("scope_user_tooltip")}
        onCheckedChange={(next) => onTeamPerUserToggle(target.skill, next)}
      >
        <span className="knob" />
      </Switch>
    );
  } else if (target.kind === "team") {
    const appr = target.skill.approval || "approved";
    headSwitch =
      appr !== "approved" ? (
        <span className={`ro-stat ${appr === "pending" ? "on" : "off"}`}>
          <span className="d" />
          {appr === "pending" ? t("team_status.pending") : t("team_status.rejected")}
        </span>
      ) : enabled ? (
        <span className="ro-stat on">
          <span className="d" />
          {t("shared_ro_on")}
        </span>
      ) : (
        <span className="ro-stat off">
          <span className="d" />
          {t("shared_ro_off")}
        </span>
      );
  }

  /* ── Team status (PRD §3.6 B): personal skill submit/withdraw/resubmit lifecycle (not rendered with flag off) ── */
  const isMineSource = target.kind === "skill" && target.skill.source_type !== "builtin";
  const sync: TeamSyncStatus | null = isMineSource && workflowCreatorEnabled ? deriveTeamSyncStatus(teamRow, detail?.content_sha256, meId) : null;
  let teamStateBlock: ReactNode = null;
  // "用对话修改" CTA uses no backend (decision 4), retained with flag off; team status/submit require flag on (nonempty sync).
  if (isMineSource && (sync || onEditInChat)) {
    const stateLabel = sync ? t(`team_status.${sync.state}` as Parameters<typeof t>[0]) : "";
    const rowId = sync?.row?.id;
    const actions: ReactNode[] = [];
    // A pending import cannot be shared (promote answers 409 skill_import_pending); the import bar above explains how to resolve it.
    const importBlocksSubmit = importStatus === "pending";
    if (!sync) {
      // Flag off: no status region / submit button, only "用对话修改".
    } else if (sync.state === "none") {
      actions.push(
        <button
          key="submit"
          type="button"
          className="btn sec sm"
          disabled={teamBusy || editing || saving || !detail?.body_md || importBlocksSubmit}
          title={importBlocksSubmit ? t("team_status.submit_blocked_import_pending") : undefined}
          onClick={() =>
            void runTeamAction(() =>
              promoteSkill({
                name: skillName,
                title: detail!.title || skillName,
                content: detail!.body_md,
              }),
            )
          }
        >
          {t("team_status.action_submit")}
        </button>,
      );
    } else if (sync.state === "behind") {
      actions.push(
        <button
          key="update"
          type="button"
          className="btn sec sm"
          disabled={teamBusy || editing || saving || !detail?.body_md || importBlocksSubmit}
          title={importBlocksSubmit ? t("team_status.submit_blocked_import_pending") : undefined}
          onClick={() =>
            void runTeamAction(() =>
              promoteSkill({
                name: skillName,
                title: detail!.title || skillName,
                content: detail!.body_md,
              }),
            )
          }
        >
          {t("team_status.action_update")}
        </button>,
      );
    } else if (sync.state === "rejected" || sync.state === "update_rejected") {
      actions.push(
        <button
          key="resubmit"
          type="button"
          className="btn sec sm"
          disabled={teamBusy || editing || saving || !detail?.body_md || importBlocksSubmit}
          title={importBlocksSubmit ? t("team_status.submit_blocked_import_pending") : undefined}
          onClick={() =>
            void runTeamAction(() =>
              promoteSkill({
                name: skillName,
                title: detail!.title || skillName,
                content: detail!.body_md,
              }),
            )
          }
        >
          {t("team_status.action_resubmit")}
        </button>,
      );
    }
    if (
      sync &&
      (sync.state === "pending" || sync.state === "update_pending" || sync.state === "rejected" || sync.state === "update_rejected") &&
      rowId
    ) {
      actions.push(
        <button
          key="withdraw"
          type="button"
          className="btn ghost sm"
          disabled={teamBusy}
          onClick={() => void runTeamAction(() => withdrawTeamSkill(rowId))}
        >
          {t("team_status.action_withdraw")}
        </button>,
      );
    }
    teamStateBlock = (
      <div
        className="drawer-teamstate legacy-skill-drawer-team"
      >
        <div className="legacy-skill-drawer-team-row"
        >
          {sync ? (
            <>
              <span className="legacy-skill-drawer-team-label"
              >
                {t("team_status.label")}
              </span>
              <span
                title={sync.reason || undefined}
                className={`legacy-skill-drawer-team-state${sync.state === "rejected" || sync.state === "update_rejected" ? " is-rejected" : ""}`}
              >
                {stateLabel}
              </span>
            </>
          ) : null}
          <span className="legacy-skill-drawer-grow" />
          {actions}
          {onEditInChat ? (
            <button key="chat" type="button" className="btn ghost sm" onClick={() => leaveEditor(() => onEditInChat(skillName))}>
              {t("team_status.action_edit_in_chat")}
            </button>
          ) : null}
        </div>
        {sync?.reason ? (
          <div className="legacy-skill-drawer-team-reason">
            {t("team_status.reason_label")}: {sync.reason}
          </div>
        ) : null}
        {sync ? <p className="drawer-note">{t("team_status.submit_hint")}</p> : null}
        {teamErr ? <SkillErrorNotice error={teamErr} /> : null}
      </div>
    );
  }

  /* ── Team skill: Copy to Mine (member improvement entry, PRD §3.6 D) + rejection reason ── */
  let teamCopyBlock: ReactNode = null;
  if (target.kind === "team") {
    teamCopyBlock = (
      <div
        className="drawer-teamcopy legacy-skill-drawer-team"
      >
        <div className="legacy-skill-drawer-team-row"
        >
          {copyState === "confirm" ? (
            <>
              <span className="legacy-skill-drawer-team-conflict">{t("team_status.copy_conflict")}</span>
              <button type="button" className="btn sec sm" disabled={teamBusy} onClick={() => void doCopy(true)}>
                {t("team_status.copy_overwrite")}
              </button>
              <button type="button" className="btn ghost sm" onClick={() => setCopyState("idle")}>
                {t("team_status.copy_cancel")}
              </button>
            </>
          ) : copyState === "done" ? (
            <span className="legacy-skill-drawer-team-done">{t("team_status.copy_done")}</span>
          ) : (
            <button type="button" className="btn sec sm" disabled={teamBusy} onClick={() => void doCopy(false)}>
              {t("team_status.action_copy")}
            </button>
          )}
        </div>
        {target.skill.rejected_reason ? (
          <div className="legacy-skill-drawer-team-reason">
            {t("team_status.reason_label")}: {target.skill.rejected_reason}
          </div>
        ) : null}
        {teamErr ? <SkillErrorNotice error={teamErr} /> : null}
      </div>
    );
  }

  /* ── body ── */
  let body: ReactNode = null;
  if (tab === "usage") {
    body = (
      <ResourceUsageDetails
        data={usage}
        loading={usageLoading}
        error={usageErr}
        onRetry={() => void loadUsage()}
        onNavigate={(href) => {
          leaveEditor(() => {
            onClose();
            if (onNavigateAway) onNavigateAway(href);
            else router.push(href);
          });
        }}
      />
    );
  } else if (err) {
    body = <SkillErrorNotice error={err} />;
  } else if (!loaded) {
    body = (
      <div className="drawer-loading">
        <span className="spin" />
        {t("loading")}
      </div>
    );
  } else if (fileView) {
    const isCode = fileView.path.endsWith(".py");
    body = (
      <>
        <button type="button" className="fileview-back" onClick={() => leaveEditor(() => setFileView(null))}>
          <ArrowLeft size={14} /> {t("drawer.back_to_files")}
        </button>
        <div className="fileview-top">
          <span className="fi">{isCode ? <FileCode size={15} /> : <FileText size={15} />}</span>
          <span className="fileview-name">{fileView.path}</span>
          <span className="fileview-size">{formatByteSize(fileView.size)}</span>
          {canEdit && !editing ? (
            <button type="button" className="btn ghost sm" disabled={saving} onClick={() => beginEdit(fileView.path, fileView.content, fileView.content_sha256 ?? undefined)}>
              {t("drawer.edit")}
            </button>
          ) : null}
          {savedAt && !editing ? <span className="fileview-saved">{t("drawer.saved")}</span> : null}
        </div>
        {authoringEnabled && !editing ? (
          <div className="fileview-actions">
            {renameTarget === null ? (
              <button
                type="button"
                className="btn ghost sm"
                disabled={saving}
                onClick={() => setRenameTarget(fileView.path.slice(fileView.path.indexOf("/") + 1))}
              >
                {t("drawer.rename_file")}
              </button>
            ) : (
              <>
                <label>
                  {t("drawer.filename")}
                  <input value={renameTarget} disabled={saving} onChange={(e) => setRenameTarget(e.target.value)} />
                </label>
                <button type="button" className="btn sec sm" disabled={saving || !renameTarget.trim()} onClick={() => void manageFile("rename")}>
                  {t("drawer.rename_file")}
                </button>
                <button type="button" className="btn ghost sm" disabled={saving} onClick={() => setRenameTarget(null)}>
                  {t("cancel")}
                </button>
                <span>{t("drawer.rename_hint")}</span>
              </>
            )}
            <button type="button" className="btn ghost sm" disabled={saving} onClick={() => void manageFile("delete")}>
              {t("drawer.delete_file")}
            </button>
          </div>
        ) : null}
        {editing ? (
          <>
            <textarea
              aria-label={editingPath ?? ""}
              disabled={saving}
              className="fileview-body"
              value={draft}
              spellCheck={false}
              onChange={(e) => setDraft(e.target.value)}
            />
            {editBar(() => void saveFile())}
          </>
        ) : (
          <pre className="fileview-body">{fileView.content}</pre>
        )}
      </>
    );
  } else if (tab === "desc") {
    body = editing ? (
      <>
        {hosted ? (
          <label className="settings-field legacy-skill-drawer-body-field">
            <span className="settings-field__label">{t("drawer.body_label")}</span>
            <textarea
              disabled={saving}
              className="settings-input legacy-skill-drawer-body"
              rows={8}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            />
          </label>
        ) : (
          <textarea
            aria-label={editingPath ?? ""}
            disabled={saving}
            className="fileview-body"
            value={draft}
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
          />
        )}
        {editBar(() => void saveBody())}
      </>
    ) : (
      <>
        {feedbackPrefill && canEdit && loaded ? (
          <FeedbackPrefillPanel
            handoff={feedbackPrefill}
            baseline={bodyRaw}
            onApply={(edited) => applyFeedbackPrefill(edited, bodyRaw)}
            onDiscard={() => { setFeedbackPrefill(null); clearFeedbackPrefill(); }}
          />
        ) : null}
        {coachDraft ? (
          <SkillCoachDraftPanel
            draft={coachDraft}
            baseline={bodyRaw}
            findings={detail?.import_report?.findings ?? []}
            locale={locale}
            onApply={(edited) => {
              applyFeedbackPrefill(edited, bodyRaw);
              setCoachDraft(null);
            }}
            onDiscard={() => setCoachDraft(null)}
          />
        ) : null}
        {canEdit ? (
          <div className="fileview-top">
            <span className="fileview-name">SKILL.md</span>
            <span className="fileview-size" />
            <button type="button" className="btn ghost sm" disabled={saving} onClick={() => beginEdit("SKILL.md", bodyRaw, detailRevision)}>
              {t("drawer.edit")}
            </button>
            {savedAt ? <span className="fileview-saved">{t("drawer.saved")}</span> : null}
          </div>
        ) : null}
        {bodyMd ? (
          <div className="dmd">
            <Markdown markdown={bodyMd} />
          </div>
        ) : (
          <div className="drawer-note">{t("drawer.body_empty")}</div>
        )}
      </>
    );
  } else if (tab === "trig") {
    body = !triggerExamples.length ? <div className="drawer-note">{t("drawer.trig_empty")}</div> : (
      <div className="trig-list">
        {triggerExamples.map((ex) => (
          <div key={ex} className="trig">
            {ex}
          </div>
        ))}
      </div>
    );
  } else if (tab === "files") {
    body = (
      <>
        {authoringEnabled ? (
          <form
            className="fileview-actions"
            onSubmit={(event) => {
              event.preventDefault();
              const path = `${newKind}/${newFilename.trim()}`;
              setCreating(true);
              setFileView({ path, content: "", size: 0, mime: "text/plain" });
              beginEdit(path, "", detailRevision);
            }}
          >
            <label>
              {t("drawer.file_kind")}
              <select value={newKind} onChange={(e) => setNewKind(e.target.value as SkillFileChange["kind"])}>
                <option value="references">{t("drawer.group_references")}</option>
                <option value="scripts">{t("drawer.group_scripts")}</option>
                <option value="evals">{t("drawer.group_evals")}</option>
              </select>
            </label>
            <label>
              {t("drawer.filename")}
              <input required value={newFilename} placeholder="nested/example.md" onChange={(e) => setNewFilename(e.target.value)} />
            </label>
            <button type="submit" className="btn sec sm" disabled={saving || !newFilename.trim()}>
              {t("drawer.create_file")}
            </button>
          </form>
        ) : null}
        {fileErr ? <SkillErrorNotice error={fileErr} /> : null}
        {references.length ? (
          <div className="fsec">
            <div className="fsec-lab">references · {references.length}</div>
            <div className="file-list">
              {references.map((f) => (
                <button key={f.name} type="button" className="file-row" onClick={() => void openFile("references", f.name)}>
                  <span className="fi">
                    <FileText size={15} />
                  </span>
                  <span className="fn">{f.name}</span>
                  <span className="fz">{formatByteSize(f.size)}</span>
                  <span className="fchev">
                    <ChevronRight size={14} />
                  </span>
                </button>
              ))}
            </div>
          </div>
        ) : null}
        {scriptFiles.length ? (
          <div className="fsec">
            <div className="fsec-lab">scripts · {scriptFiles.length}</div>
            <div className="file-list">
              {scriptFiles.map((f) => (
                <button key={f.name} type="button" className="file-row" onClick={() => void openFile("scripts", f.name)}>
                  <span className="fi">
                    <FileCode size={15} />
                  </span>
                  <span className="fn">{f.name}</span>
                  <span className="fz">{formatByteSize(f.size)}</span>
                  <span className="fchev">
                    <ChevronRight size={14} />
                  </span>
                </button>
              ))}
            </div>
          </div>
        ) : null}
        {evalFiles.length ? (
          <div className="fsec">
            <div className="fsec-lab">evals · {evalFiles.length}</div>
            <div className="file-list">
              {evalFiles.map((file) => (
                <button key={file.name} type="button" className="file-row" onClick={() => void openFile("evals", file.name)}>
                  <span className="fi">
                    <FileText size={15} />
                  </span>
                  <span className="fn">{file.name}</span>
                  <span className="fz">{formatByteSize(file.size)}</span>
                  <span className="fchev">
                    <ChevronRight size={14} />
                  </span>
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </>
    );
  } else if (tab === "evals") {
    body = !hasEvals ? <div className="drawer-note">{t("drawer.evals_empty")}</div> : (
      <>
        <div className="drawer-note legacy-skill-drawer-evals-note">
          {t("drawer.evals_count", { count: evalsCount })}
        </div>
        {evalsErr ? (
          <SkillErrorNotice error={evalsErr} />
        ) : evalsContent === null ? (
          <div className="drawer-loading">
            <span className="spin" />
            {t("loading")}
          </div>
        ) : (
          <pre className="fileview-body">{evalsContent}</pre>
        )}
      </>
    );
  }

  /* ── badges ── */
  const badges: ReactNode[] = [];
  if (target.kind === "team") {
    badges.push(
      <span key="from" className="sbadge from">
        {t("badge_from", {
          name: teamSkillOwnerName(target.skill, ownerFallback),
        })}
      </span>,
    );
    badges.push(
      // Verdict vocabulary: pass/fail/partial/skipped/error (wf-shared VERDICT); only pass counts as verified.
      (teamDetail ?? target.skill).verdict === "pass" ? (
        <span key="v" className="sbadge verified">
          <CharIconText ch="✓">{t("badge_verified")}</CharIconText>
        </span>
      ) : (
        <span key="v" className="sbadge unverified">
          {t("badge_unverified")}
        </span>
      ),
    );
  }

  const content = (
    <>
      {hosted ? (headSwitch ? <CanvasToolbarActions>{headSwitch}</CanvasToolbarActions> : null) : <div className="drawer-head">
        <div className="drawer-toprow">
          <h2 className="drawer-title">{title}</h2>
          <div className="drawer-headctrls">
            {headSwitch}
            {deletable ? (
              <button
                type="button"
                className="srow-ib danger"
                title={t("delete_title")}
                aria-label={`${t("delete_title")} ${title}`}
                onClick={() =>
                  leaveEditor(() => {
                    if (target.kind === "skill") onDelete!(target.skill);
                  })
                }
              >
                <Trash size={15} />
              </button>
            ) : null}
            <button type="button" className="drawer-x" aria-label={t("drawer.close")} onClick={close}>
              <X size={15} />
            </button>
          </div>
        </div>
        <div className="drawer-metaline">
          <span className="dm-slug">{skillName}</span>
          <span>·</span>
          <span>{sourceLabel}</span>
          {target.kind === "skill" && target.skill.version ? (
            <>
              <span>·</span>
              <span>v{target.skill.version}</span>
            </>
          ) : null}
          {sizeBytes ? (
            <>
              <span>·</span>
              <span>{formatByteSize(sizeBytes)}</span>
            </>
          ) : null}
          {createdAt ? (
            <>
              <span>·</span>
              <span>{t("drawer.created_at", { date: formatDateYMD(createdAt) })}</span>
            </>
          ) : null}
        </div>
        {badges.length ? <div className="drawer-badges">{badges}</div> : null}
      </div>}

      {importStatus === "pending" || importStatus === "overridden" ? (
        <div className={`imp-bar${importStatus === "pending" ? " pend" : " ovr"}`}>
          <div className="imp-bar-t">
            {importStatus === "pending" ? ti("drawer_pending_title") : ti("drawer_overridden_title")}
          </div>
          <div className="imp-bar-d">
            {importStatus === "pending" ? ti("drawer_pending_desc") : ti("drawer_overridden_desc")}
          </div>
          {(importStatus === "pending" || importStatus === "overridden") && canEdit ? (
            <div className="imp-bar-actions">
              {coachEnabled ? (
                <button
                  type="button"
                  className="btn pri sm"
                  disabled={!loaded || coachBusy}
                  // Panel renders only on "说明" outside editing mode; without switching there, requests still run but panel stays hidden,
                  // consuming one of this button's 3 daily uses even when successful output is unseen (review P1-3).
                  onClick={() =>
                    leaveEditor(() => {
                      setFileView(null);
                      setTab("desc");
                      void askCoach();
                    })
                  }
                >
                  {coachBusy ? tc("asking") : tc("ask")}
                </button>
              ) : null}
              <button
                type="button"
                className={coachEnabled ? "btn sec sm" : "btn pri sm"}
                // Do not open editor before details return; draft would be empty, so saving clears the body (review P2-1).
                disabled={!loaded}
                onClick={() =>
                  leaveEditor(() => {
                    // Users may be on another tab / file view; without switching, clicking appears ineffective,
                    // and worse, file view holds SKILL.md draft while save silently skips (review P2-2).
                    setFileView(null);
                    setTab("desc");
                    beginEdit("SKILL.md", detail?.body_raw ?? detail?.body_md ?? "", detailRevision);
                  })
                }
              >
                {ti("go_edit")}
              </button>
              {importStatus === "pending" ? (
                <button type="button" className="btn ghost sm" disabled={overriding} onClick={() => void doOverride()}>
                  {overriding ? ti("override_busy") : ti("override")}
                </button>
              ) : null}
            </div>
          ) : null}
          {detail?.import_report ? (
            <>
              <p className="imp-verdict">{skillImportSummary(detail.import_report, ti)}</p>
              <SkillImportFindings report={detail.import_report} locale={locale} />
            </>
          ) : null}
        </div>
      ) : null}
      {coachErr ? (
        <div className="imp-bar-err">
          <SkillErrorNotice error={coachErr} />
        </div>
      ) : null}

      {(isTeam ? teamDetail?.script_execution : detail?.script_execution) === "disabled" ? (
        <div className="drawer-note">{t("drawer.execution_disabled")}</div>
      ) : null}
      {isMineSource ? <p className="drawer-note">{t("drawer.personal_usage")}</p> : null}
      {target.kind === "team" &&
      canEdit &&
      (teamDetail?.approval ?? target.skill.approval) === "approved" &&
      (teamDetail?.visibility ?? target.skill.visibility) === "team_wide" ? (
        <div className="drawer-note">{t("drawer.team_live_edit_hint")}</div>
      ) : null}
      {teamStateBlock}
      {teamCopyBlock}

      {!isTeam && detail?.allowed_tools.length ? (
        <div className="drawer-toolrow">
          {/* ⚠ Say "使用的工具", never "允许": allowed_tools is declarative, not enforced (audit §3-4). */}
          <span className="tl-lab">{t("drawer.tools_label")}</span>
          {detail.allowed_tools.map((tool) => (
            <span key={tool} className="tool-chip">
              {tool}
            </span>
          ))}
        </div>
      ) : null}

      {tabs.length > 1 && !fileView ? (
        <Tabs value={tab} onValueChange={(id) => leaveEditor(() => setTab(id))} idBase={tabsId}>
          <TabList appearance={hosted ? "segmented" : "underline"} className="drawer-tabs" aria-label={title}>
            {tabs.map(({ id, label }) => (
              <UiTab key={id} value={id} aria-controls={`${tabsId}-tabpanel`} className={tab === id ? "on" : ""}>
                {label}
              </UiTab>
            ))}
          </TabList>
        </Tabs>
      ) : null}

      <div
        id={tabs.length > 1 && !fileView ? `${tabsId}-tabpanel` : undefined}
        role={tabs.length > 1 && !fileView ? "tabpanel" : undefined}
        aria-labelledby={tabs.length > 1 && !fileView ? `${tabsId}-tab-${tab}` : undefined}
        className="drawer-body"
      >
        {saveErr && !editing ? <SkillErrorNotice error={saveErr} /> : null}
        {body}
      </div>
      {/* Inside the shell, confirmation becomes the drawer's child dialog: Esc closes only confirmation. */}
      {confirmDialog}
    </>
  );
  return hosted ? <section className="sset legacy-skill-drawer-hosted" aria-label={title}>{content}</section> : (
    <DialogShell presentation="center" onClose={close} label={title} mount="inline" backdropClassName="drawer-scrim" panelClassName="drawer">
      {content}
    </DialogShell>
  );
}

/** Splits SKILL.md into its frontmatter block (kept verbatim, including the closing line) and the instructions. */
export function splitSkillManifest(raw: string): { front: string; body: string } {
  const match = /^---\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/.exec(raw);
  if (!match) return { front: "", body: raw };
  return { front: match[0], body: raw.slice(match[0].length) };
}

"use client";

import dynamic from "next/dynamic";
import { copyComposerDocumentRevision, sameDocumentRevisionTarget, type ComposerDocumentRevision, type DocumentRevisionTarget, type DocumentRevisionStageRequest, type DocumentRevisionStageResult } from "@/lib/composer-document-revision";

import { appFetch as fetch, appPath } from "@/lib/base-path";

import { useEffect, useId, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { useLocale, useTranslations } from "next-intl";

import {
  PRESIGN_THRESHOLD,
  UPLOAD_ACCEPT,
  UPLOAD_MAX_BYTES,
  UPLOAD_MAX_PER_MESSAGE,
  uploadFile,
  validateUpload,
} from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import { fileError } from "@/lib/file-error";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { googlePickerErrorKey, pickDriveFiles, pickedFilesAsText, prepareGooglePicker } from "@/lib/google-picker";
import {
  ConfirmExpiredError,
  PresignExpiredError,
  UploadHttpError,
  UploadNetworkError,
  UploadTerminalError,
} from "@/lib/upload-xhr";
import { dispatchRememberLine } from "@/lib/remember-slash";
import { useAutocomplete } from "@/lib/use-autocomplete";
import { useComposerCapabilities } from "@/lib/use-composer-capabilities";
import { completeComposerScope, composerDraftScopeSignature } from "@/lib/composer-persistence-scope";
import { registerComposerDraftHandoff } from "@/lib/composer-draft-handoff";
import {
  useComposerLayoutMode,
  type ComposerLayoutMode,
} from "@/lib/composer-layout";
import { useTextareaAutosize } from "@/lib/use-textarea-autosize";
import { useComposerVisualViewport } from "@/lib/use-composer-visual-viewport";
import { track, trackComposerEvent } from "@/lib/metrics";

import type { ComposerSubmissionWireSnapshot } from "@/lib/composer-submission-recovery";
import type { AttachmentRef } from "@/lib/types";
import {
  AttachmentChip,
  type AttachmentErrorKind,
  type AttachmentItem,
} from "./attachment-chip";
import { DialogShell } from "@/components/ui/dialog";
import { copyDiagnostics, ErrorDiagnosticsInline } from "@/components/error-notice";
import { errorDiagnosticsFrom } from "@/lib/error-presentation";
import { toast } from "@/lib/use-toast";
import { ModelPicker } from "./model-picker";
import { EntitlementReadOnlyChip } from "./entitlement-banner";
import { RevisionTargetBar } from "./revision-target-bar";
import { PlanIcon, type PlanIconName } from "./plan-card";
import { usePlanningModePref } from "@/lib/planning-mode-pref";
import { COMPOSER_MENU_MAX_HEIGHT, focusComposerFallback, useComposerFocusFallback } from "@/lib/composer-menu";
import { MENU_DESCRIPTION_ATTR, MenuFloating, MenuItemReason, useMenu, useMenuItem } from "@/components/ui/menu";
import {
  canonicalizeComposerAttachmentRef,
  clearComposerOrdinaryDraft,
  ComposerOrdinaryDraftInvalidError,
  discardComposerOrdinaryDraft,
  readComposerOrdinaryDraft,
  readComposerInputDraft,
  writeComposerInputDraft,
  clearComposerInputDraft,
  readComposerReferenceDraft,
  removeComposerDraftReferences,
  writeComposerOrdinaryDraft,
  type ComposerDraftScope,
  type ComposerOrdinaryDraft,
  type ComposerReferenceDraft,
} from "@/lib/composer-ordinary-draft";
import { ComposerReferencePreview } from "./composer-reference-preview";
import { ComposerReferenceTransferBoundary } from "./composer-reference-transfer-boundary";
import { spaceHeader } from "@/lib/space-storage";
import { MAX_COMPOSER_RUNTIME_ATTACHMENTS } from "@/lib/composer-attachment-refs";
import { randomUuid } from "@/lib/random-id";
import {
  ComposerRecoveryUnavailableError,
  ComposerPersistenceVersionError,
  composerPersistenceNeedsUpgrade,
} from "@/lib/composer-local-persistence-db";
import {
  clearComposerTransientDraft,
  forgetComposerTransientDraft,
  readComposerInputTransientDraft,
  retainComposerInputTransientDraft,
  type ComposerInputTransientDraft,
} from "@/lib/composer-transient-draft";
import {
  inputDocumentDisplayText, inputDocumentFromText, inputDocumentTextContent, parseInputDocument,
  validateInputDocumentTask, type InputDocument,
} from "@/lib/composer-input-document";
import { copyComposerReferenceSnapshot } from "@/lib/composer-reference-transfer";
import {
  ArrowUp as LucideArrowUp,
  Plus as LucidePlus,
  Square as LucideSquare,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { ComposerNotices, orderComposerNotices, type ComposerNoticeItem } from "./composer-notices";
import { useComposerFileDrop, type ComposerDropVerdict } from "./use-composer-file-drop";
import { CharIcon } from "@/components/char-icon";

const ReferenceEditor = dynamic(() => import("./composer-reference-editor").then((module) => module.ComposerReferenceEditor), { ssr: false });
type InputDraftContent = Pick<ComposerOrdinaryDraft, "message" | "attachments"> & { input_document?: InputDocument; documentRevision?: ComposerDocumentRevision };
function inputDraftContent(draft: InputDraftContent): InputDraftContent {
  return {
    message: draft.message,
    attachments: draft.attachments.map((item) => ({ ...item, ...(item.bundleArtifactIds ? { bundleArtifactIds: [...item.bundleArtifactIds] } : {}) })),
    ...(draft.input_document ? { input_document: parseInputDocument(draft.input_document) } : {}),
    ...(draft.documentRevision ? { documentRevision: copyComposerDocumentRevision(draft.documentRevision) } : {}),
  };
}
function inputTransient(draft: InputDraftContent & { expectedRevision: string | null }): ComposerInputTransientDraft {
  const snapshot = { ...copyComposerReferenceSnapshot({ schemaVersion: 2, ...draft, input_document: draft.input_document ?? inputDocumentFromText(draft.message) }), expectedRevision: draft.expectedRevision };
  return draft.documentRevision ? { ...snapshot, schemaVersion: 3, documentRevision: copyComposerDocumentRevision(draft.documentRevision) }
    : draft.input_document ? snapshot : draft;
}
// ThetaCancelButton retired with chat-redesign Phase 1.2 — inline ⏹ button
// reinstated below; cancel UX gets a v8-token-aware redesign in Phase 1.5.

/**
 * Lets the page discard this composer's persisted draft before an explicit
 * “New work” reset remounts it. Returns false (and keeps the draft) when a
 * pending send or recovery owns it.
 */
export interface ComposerDraftControl {
  discardForNewWork: () => Promise<boolean>;
  stageDocumentRevision: (request: DocumentRevisionStageRequest) => Promise<DocumentRevisionStageResult>;
  /** Chat turn feedback D5: the failure card's Retry runs the composer's own Replay, with the same
   *  single-flight and mutation guards. Returns false when the composer refused to start it. */
  requestReplay: () => boolean;
  /** What the composer holds right now, so a caller can stage a message only into an empty composer. */
  readInput: () => { text: string; hasAttachments: boolean; hasInputDocument: boolean };
}

interface Props {
  /** Native input supersedes a pending result-view selection, even for identical text. */
  onInputInteraction?: () => void;
  /** Server-projected authoring capability; selection and submission remain independently checked. */
  referenceInputEnabled?: boolean;
  /** Attachments include uploaded-file metadata (fileId + filename + size + mime).
   * A string-id array loses display information, degrading bubble chips to "📎 art_xxx". */
  onSend: (
    text: string,
    attachments?: AttachmentRef[],
    draftRevision?: string | null,
    submissionContext?: ComposerDraftSubmissionContext | null,
  ) => void | boolean | Promise<void | boolean>;
  onResolveDraftSubmissionProtocol?: (
    actionKind: "start" | "steer",
    attachments: AttachmentRef[],
    hasReferenceInput?: boolean,
  ) => Promise<ComposerDraftWriteProtocol | null>;
  /** Capture the editing target before any asynchronous draft persistence or auth preflight. */
  onCaptureSubmissionWireSnapshot?: (
    actionKind: "start" | "steer", hasReferenceInput: boolean,
    documentRevisionTarget?: DocumentRevisionTarget,
  ) => ComposerSubmissionWireSnapshot | null;
  disabled?: boolean;
  placeholder?: string;
  /** user_texts already sent in this session; prioritized for Tab completion (#88 HITL Tab completion). */
  sessionTexts?: string[];
  /** M1.4 Stop button: switch send to ⏹ stop while the current turn runs; parent observes runner.running. */
  running?: boolean;
  /** Durable Stop accepted; wait for the worker terminal frame. */
  stopRequested?: boolean;
  /** Stop callback; parent persists the cancel request and retains SSE authority. */
  onCancel?: () => void | boolean | Promise<void | boolean>;
  /** Sprint chat-v4-streaming Wave 4 — "换个问法" prefill. When parent passes
   *  a non-empty string (typically after HITLPickerCard dismiss), the
   *  composer overwrites its current input with this text + focuses textarea.
   *  Parent should clear it (pass empty/null) after one frame to avoid
   *  re-prefill on unrelated re-renders. */
  prefillText?: string | null;
  /** Each increase submits the current input through the ordinary Send path and its guards, as if Send were pressed
   *  (for example after the Chat page saved the canvas edits that blocked the previous send). */
  submitRequest?: number;
  /** Exact, catalog-resolved reference for an explicit "use this Agent" action. */
  prefillInputDocument?: InputDocument | null;
  onPrefillInputDocumentConsumed?: (disposition: "applied" | "discarded") => void;
  /** Applied/discarded are acknowledged; no callback means hydration deferred it. */
  onPrefillConsumed?: (disposition: "applied" | "discarded") => void;
  /** Visible state feedback associated with a parent-driven prefill. */
  statusMessage?: string | null;
  /** file-management-v2 P2 "在新会话中使用": an **existing** artifact from the files page
   *  becomes a done chip directly (no byte reupload). Consumed once, like prefillText. */
  prefillAttachments?: AttachmentRef[] | null;
  /** Acknowledge the exact refs merged into the current draft so event-style
   * producers can clear their queue without reattaching old results later. */
  onPrefillAttachmentsConsumed?: (attachments: AttachmentRef[]) => void;
  /** Phase 1.4 Steer mode — when present + running=true, submitting text
   *  routes through onSteer instead of starting a new turn. Backend buffers
   *  the message into the active turn's steer queue. Composer stays input-
   *  enabled in this state so the user can keep nudging mid-turn. */
  onSteer?: (
    text: string,
    attachments?: AttachmentRef[],
    draftRevision?: string | null,
    submissionContext?: ComposerDraftSubmissionContext | null,
  ) => void | boolean | Promise<void | boolean>;
  /** The last turn's question: the source the failure card's Retry replays through `draftControlRef`
   * (Chat Cards Spec D4: the composer itself keeps Send, disabled on empty input). */
  lastUserText?: string | null;
  lastUserAttachments?: AttachmentRef[];
  /** Replay uses the server-owned canonical source Turn, never a fresh start. */
  onReplay?: () => void | boolean | Promise<void | boolean>;
  /** Licensing §13.2 — entitlement read-only (over_grace / suspended). Disables send +
   *  the attach/file affordances, swaps the placeholder to “发送已停用”, and shows the
   *  persistent "只读模式" chip. Orthogonal to `disabled` (which is the runner-busy gate). */
  readOnly?: boolean;
  /** Submission recovery/checking fence. Keeps content focusable/copyable but blocks mutation. */
  recoveryLocked?: boolean;
  draftPersistenceScope?: ComposerDraftScope | null;
  /** Reports a durable marker found with the ordinary draft; it never sends. */
  onPendingActionHydrated?: (draft: ComposerOrdinaryDraft) => void;
  /** Re-read exact persisted state after a recovery action sent outside this component. */
  draftReconcileEpoch?: number;
  /** Legacy/default-off Chat must remain usable when IndexedDB is unavailable. */
  draftPersistenceFailureMode?: "block" | "warn";
  /** Focus the textarea when this composer instance mounts after explicit user intent. */
  autoFocus?: boolean;
  /** Hide only the view while preserving draft and upload state. */
  hidden?: boolean;
  /** Stable, low-cardinality placement label for responsive-layout telemetry. */
  surface?: "home" | "workbench";
  /** Registered while mounted; see {@link ComposerDraftControl}. */
  draftControlRef?: RefObject<ComposerDraftControl | null>;
  /** File drop target (workbench supplies the conversation column); defaults to the input itself. */
  fileDropTargetRef?: RefObject<HTMLElement | null>;
  /** Parent's known reason sending is unavailable (e.g. no usable Space), shown in the status row below the input. */
  sendBlockedReason?: string | null;
}

export type ComposerDraftWriteProtocol = "legacy_v0" | "terminal_v1";

export interface ComposerDraftSubmissionContext {
  wireSnapshot?: ComposerSubmissionWireSnapshot;
  writeProtocol: ComposerDraftWriteProtocol;
  persistenceAvailable: boolean;
  draftMessage?: string;
  referenceInputDocument?: InputDocument;
  referenceScopeKey?: string;
  /** Reference steer journal settles only after the exact local draft CAS. */
  finalizeReferenceSubmission?: () => Promise<void>;
}

type AcceptedDraftCleanup = {
  scope: ComposerDraftScope;
  scopeSignature: string;
  revision: string;
  transient: InputDraftContent & { expectedRevision: string };
};

function mkLocalId(): string {
  return randomUuid();
}

function attachmentIdentity(attachment: Pick<AttachmentRef, "fileId" | "bundleArtifactIds">): string {
  return JSON.stringify([
    attachment.fileId,
    ...(attachment.bundleArtifactIds ?? []),
  ]);
}

function attachmentRefToItem(attachment: AttachmentRef): AttachmentItem {
  return {
    localId: mkLocalId(),
    fileId: attachment.fileId,
    filename: attachment.filename,
    size: attachment.size,
    mime: attachment.mime,
    status: "done",
    ...(attachment.selectedContext ? { selectedContext: attachment.selectedContext } : {}),
    kind: attachment.bundleArtifactIds?.length ? "bundle" : "file",
    ...(attachment.referenceKind
      ? { referenceKind: attachment.referenceKind }
      : {}),
    ...(attachment.bundleArtifactIds?.length
      ? { bundleArtifactIds: [...attachment.bundleArtifactIds] }
      : {}),
  };
}

function attachmentItemToRef(item: AttachmentItem): AttachmentRef {
  const attachment = canonicalizeComposerAttachmentRef({
    fileId: item.fileId as string,
    filename: item.filename,
    size: item.size,
    mime: item.mime,
    ...(item.referenceKind ? { referenceKind: item.referenceKind } : {}),
    ...(item.selectedContext ? { selectedContext: item.selectedContext } : {}),
    ...(item.bundleArtifactIds?.length
      ? { bundleArtifactIds: [...item.bundleArtifactIds] }
      : {}),
  });
  if (!attachment) {
    throw new ComposerOrdinaryDraftInvalidError(
      "composer attachment invalid",
    );
  }
  return attachment;
}

function ordinaryDraftFingerprint(
  message: string,
  attachments: AttachmentRef[],
  input_document?: InputDocument,
  documentRevision?: ComposerDocumentRevision,
): string {
  return JSON.stringify({ message, attachments, ...(input_document?.nodes.some((node) => node.type === "reference") ? { input_document } : {}), ...(documentRevision ? { documentRevision } : {}) });
}

/** Three planning states; order matches the menu (design §3). */
const PLANNING_MODES = ["auto", "planning", "off"] as const;
/** D10: how long the existing chip is outlined when the same file is added again. */
const DUPLICATE_FLASH_MS = 600;
type PlanningMode = (typeof PLANNING_MODES)[number];

/** P2 batch 2: Lucide glyphs. The character span stays in the DOM for the composer layout CSS and is hidden by the refreshed styles. */
const COMPOSER_REFRESH_ICONS = {
  add: LucidePlus,
  send: LucideArrowUp,
  stop: LucideSquare,
} as const;

function ComposerGlyph({ kind }: { kind: "add" | "send" | "stop" }) {
  const legacy = { add: "＋", send: "→", stop: "■" }[kind];
  return (
    <>
      <span className="composer-v2__legacy-glyph" aria-hidden="true">{legacy}</span>
      <LucideGlyph icon={COMPOSER_REFRESH_ICONS[kind]} size={18} className="composer-v2__refresh-glyph" aria-hidden="true" />
    </>
  );
}

/** Three states → copy key / icon / trigger modifier class. Gray (`is-muted`) is for off: a legitimate
 *  user choice, not an error; **no red** (reserved for terminal failures, explicitly discussed in design §3). */
const PLANNING_META: Record<PlanningMode, { key: string; desc: string; icon: PlanIconName; cls: string }> = {
  auto: { key: "planning_mode_auto", desc: "planning_mode_auto_desc", icon: "zap", cls: "" },
  planning: { key: "planning_mode_on", desc: "planning_mode_on_desc", icon: "list", cls: " is-active" },
  off: { key: "planning_mode_off", desc: "planning_mode_off_desc", icon: "slash", cls: " is-muted" },
};

function PlanningControl({
  compact = false,
  disabled = false,
  onToggle,
  fallbackFocusRef,
}: {
  compact?: boolean;
  disabled?: boolean;
  onToggle?: (enabled: boolean) => void;
  fallbackFocusRef?: RefObject<HTMLElement | null>;
}) {
  const t = useTranslations("composer");
  const [mode, setMode] = usePlanningModePref();
  // ★2026-08-14: two-state switch → three-state selector (Claude Design 8b6a92a0,
  // `chat-ux-2026-08/计划三态选择器.html`, direction 1c). Replace the whole old control instead of patching it:
  // old `active = mode === "planning"` + `setMode(enabled ? "planning" : "auto")` had two problems:
  //   ① `off` and `auto` rendered identically; users could not tell the state before clicking;
  //   ② in `off`, `enabled = !active = true` → **one click jumped from never plan to force a plan**,
  //     exactly opposite to the user's intent.
  // A radio menu expresses three states; **no path jumps from off to planning in one step**.
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const returnFocusFallback = useComposerFocusFallback(wrapRef, fallbackFocusRef);
  const current = (mode as PlanningMode) in PLANNING_META ? (mode as PlanningMode) : "auto";
  const cur = PLANNING_META[current];
  const label = t(cur.key);
  // ui/menu handles open/close, outside click/Esc, arrows, positioning and focus restoration, matching the model picker: prefer opening upward,
  // clamp to the visible viewport with a soft keyboard, and fall back to the input when the trigger is disabled or unmounted.
  const menu = useMenu({
    open,
    onOpenChange: (next) => {
      if (next && disabled) return;
      setOpen(next);
    },
    placement: "top-start",
    maxHeight: COMPOSER_MENU_MAX_HEIGHT,
    selectedIndex: PLANNING_MODES.indexOf(current),
    returnFocusFallback,
    // Narrow screens: each item has an explanation; below the threshold, use a bottom sheet with a heading.
    sheetTitle: t("planning_mode_sheet_title"),
  });

  useEffect(() => {
    if (disabled && open) setOpen(false);
  }, [disabled, open]);

  return (
    <div ref={wrapRef} className="composer-v2__planning-wrap">
      <button
        ref={menu.refs.setReference}
        type="button"
        className={`composer-v2__planning${cur.cls}${compact ? " is-compact" : ""}`}
        // ★Omit `aria-pressed`: it asserts two states, incorrect for three (design §5). useMenu supplies aria-haspopup.
        aria-label={t("planning_mode_trigger", { mode: label })}
        title={t("planning_mode_trigger", { mode: label })}
        disabled={disabled}
        {...menu.getReferenceProps()}
      >
        <PlanIcon name={cur.icon} size={compact ? 16 : 14} />
        {compact ? null : <span>{label}</span>}
        {compact ? null : (
          <span className="composer-v2__planning-cv" aria-hidden="true">
            <PlanIcon name="chevronDown" size={10} />
          </span>
        )}
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({
            className: "composer-v2__planning-menu",
            style: { width: 236, boxSizing: "border-box", maxHeight: COMPOSER_MENU_MAX_HEIGHT },
          })}
        >
          {PLANNING_MODES.map((m) => (
            <PlanningMenuItem
              key={m}
              mode={m}
              on={m === mode}
              onSelect={() => {
                setMode(m);
                onToggle?.(m === "planning");
              }}
            />
          ))}
        </div>
      </MenuFloating>
    </div>
  );
}

/**
 * Comfortable-layout "+" when Google Drive selection is available: upload a file or pick
 * Drive files (inserted into the input as plain "name link" lines). Without Google the
 * "+" stays a plain upload button.
 */
function ComposerAddMenu({
  disabled,
  uploadDisabled,
  fallbackFocusRef,
  onUpload,
  onPickGoogleDrive,
}: {
  disabled: boolean;
  uploadDisabled: boolean;
  fallbackFocusRef?: RefObject<HTMLElement | null>;
  onUpload: () => void;
  onPickGoogleDrive: () => void;
}) {
  const t = useTranslations("composer");
  const tDrive = useTranslations("google_drive");
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const returnFocusFallback = useComposerFocusFallback(wrapRef, fallbackFocusRef);
  const menu = useMenu({
    open,
    onOpenChange: (next) => {
      if (next && disabled) return;
      setOpen(next);
    },
    placement: "top-start",
    maxHeight: COMPOSER_MENU_MAX_HEIGHT,
    returnFocusFallback,
    sheetTitle: t("tools_title"),
  });
  return (
    <div ref={wrapRef} className="composer-v2__planning-wrap">
      <button
        ref={menu.refs.setReference}
        type="button"
        className="composer-v2__tool-button"
        title={t("add_menu_title")}
        disabled={disabled}
        aria-label={t("add_menu_label")}
        {...menu.getReferenceProps()}
      >
        <ComposerGlyph kind="add" />
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({
            className: "composer-v2__planning-menu",
            style: { width: 236, boxSizing: "border-box", maxHeight: COMPOSER_MENU_MAX_HEIGHT },
          })}
        >
          <ComposerAddMenuItem
            label={t("upload_file")}
            disabled={uploadDisabled}
            disabledReason={uploadDisabled ? t("upload_file_disabled_reason") : undefined}
            onSelect={onUpload}
          />
          <ComposerAddMenuItem label={tDrive("composer_entry")} description={tDrive("composer_entry_hint")} onSelect={onPickGoogleDrive} />
        </div>
      </MenuFloating>
    </div>
  );
}

/** Insert picked "name link" lines at the caret (or the end), each on its own line. */
export function insertPickedLines(previous: string, text: string, caret: number | null): string {
  const at = caret === null || caret < 0 || caret > previous.length ? previous.length : caret;
  const before = previous.slice(0, at);
  const after = previous.slice(at);
  const lead = before && !before.endsWith("\n") ? "\n" : "";
  const trail = after && !after.startsWith("\n") ? "\n" : "";
  return `${before}${lead}${text}${trail}${after}`;
}

function ComposerAddMenuItem({
  label,
  description,
  disabled = false,
  disabledReason,
  onSelect,
}: {
  label: string;
  description?: string;
  disabled?: boolean;
  /** Persistent second line explaining why a disabled item cannot run (menu copy spec). */
  disabledReason?: string;
  onSelect: () => void;
}) {
  const itemProps = useMenuItem({ disabled, onSelect });
  const reasonId = useId();
  const reason = disabled ? disabledReason : undefined;
  return (
    <button type="button" className="composer-v2__planning-item" {...itemProps} aria-describedby={reason ? reasonId : undefined}>
      <span className="composer-v2__planning-item-tx">
        <span className="n">{label}</span>
        {description ? (
          <span className="d" {...{ [MENU_DESCRIPTION_ATTR]: "" }}>
            {description}
          </span>
        ) : null}
        {reason ? <MenuItemReason id={reasonId}>{reason}</MenuItemReason> : null}
      </span>
    </button>
  );
}

function PlanningMenuItem({ mode, on, onSelect }: { mode: PlanningMode; on: boolean; onSelect: () => void }) {
  const t = useTranslations("composer");
  const meta = PLANNING_META[mode];
  const itemProps = useMenuItem({ role: "menuitemradio", checked: on, onSelect });
  // Behavior batch A: mark descriptions so the menu switches to a bottom sheet below the narrow-screen threshold.
  return (
    <button
      type="button"
      data-active={on ? "true" : undefined}
      data-mode={mode}
      className={`composer-v2__planning-item${on ? " is-on" : ""}`}
      {...itemProps}
    >
      <span className="composer-v2__planning-item-ic">
        <PlanIcon name={meta.icon} size={16} />
      </span>
      <span className="composer-v2__planning-item-tx">
        <span className="n">{t(meta.key)}</span>
        <span className="d" {...{ [MENU_DESCRIPTION_ATTR]: "" }}>
          {t(meta.desc)}
        </span>
      </span>
      {on ? (
        <span className="composer-v2__planning-item-ck" aria-hidden="true">
          <PlanIcon name="check" size={14} />
        </span>
      ) : null}
    </button>
  );
}

function ComposerToolsSheet({
  open,
  dialogId,
  triggerRef,
  planning,
  onUploadChange,
  uploadDisabled,
  onPickGoogleDrive,
  onPlanningToggle,
  onClose,
}: {
  open: boolean;
  dialogId: string;
  triggerRef: React.RefObject<HTMLButtonElement | null>;
  planning: boolean;
  onUploadChange: (event: React.ChangeEvent<HTMLInputElement>) => void;
  uploadDisabled: boolean;
  /** Present only while Google Drive selection is available to this member's team. */
  onPickGoogleDrive?: () => void;
  onPlanningToggle: (enabled: boolean) => void;
  onClose: () => void;
}) {
  const t = useTranslations("composer");
  const tDrive = useTranslations("google_drive");
  const uploadRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const composerRoot = triggerRef.current?.closest("[data-composer-surface]") ?? null;
    // On close the trigger may be disabled (sending/read-only), defeating DialogShell focus restoration; fall back to the input in the original order.
    return () => focusComposerFallback(composerRoot, triggerRef.current, previous);
  }, [open, triggerRef]);

  if (!open) return null;

  return (
    <DialogShell
      presentation="center"
      onClose={onClose}
      label={t("tools_title")}
      backdropClassName="composer-v2__sheet-backdrop"
      // FloatingOverlay's inline top/bottom overrides the stylesheet's visual-viewport values; restore them here.
      backdropStyle={{
        top: "var(--bh-visual-viewport-top, 0px)",
        bottom: "var(--bh-visual-viewport-bottom, 0px)",
      }}
      panelClassName="composer-v2__sheet"
      panelProps={{ id: dialogId }}
    >
      <div className="composer-v2__sheet-head">
        <strong>{t("tools_title")}</strong>
        <button type="button" onClick={onClose} aria-label={t("tools_close")}><CharIcon ch="×" aria-hidden /></button>
      </div>
      <input
        ref={uploadRef}
        disabled={uploadDisabled}
        type="file"
        multiple
        accept={UPLOAD_ACCEPT}
        tabIndex={-1}
        aria-hidden="true"
        style={{ display: "none" }}
        onChange={(event) => {
          onUploadChange(event);
          onClose();
        }}
      />
      <button
        type="button"
        className="composer-v2__sheet-row"
        disabled={uploadDisabled}
        onClick={() => {
          // This input lives inside the non-inert dialog portal, so the
          // native picker remains in the trusted click task on mobile.
          uploadRef.current?.click();
        }}
      >
        <ComposerGlyph kind="add" />
        {t("upload_file")}
      </button>
      {onPickGoogleDrive ? (
        <button
          type="button"
          className="composer-v2__sheet-row"
          onClick={() => {
            onClose();
            onPickGoogleDrive();
          }}
        >
          <ComposerGlyph kind="add" />
          {tDrive("composer_entry")}
        </button>
      ) : null}
      {/* ★2026-08-14: remove the planning row from the ＋ panel (design §5): the three-state trigger now stays in the toolbar
          in **all modes** (including compact/ultra's 44×44 icon form). This row became a second source
          for the same state: two controls expressing one thing recreate the need for users to reason
          about state in two places. Once the entry returns to the toolbar, it must be absent here. */}
    </DialogShell>
  );
}

export function V2Composer(props: Props) {
  const auth = useOptionalAuthSession();
  const referenceInputEnabled = props.referenceInputEnabled ?? auth?.me?.capabilities?.composer?.reference_input === true;
  const scopeKey = composerDraftScopeSignature(props.draftPersistenceScope ?? null);
  // A new draft scope gets a fresh composer instance, except for the first
  // scope adopted after mounting without one: the home composer mounts while
  // the Space list is still loading, and remounting it when the scope resolves
  // would drop focus and anything already typed.
  const [scopeIdentity, setScopeIdentity] = useState({ signature: scopeKey, generation: 0 });
  let identity = scopeIdentity;
  if (scopeIdentity.signature !== scopeKey) {
    identity = {
      signature: scopeKey,
      generation: scopeIdentity.signature ? scopeIdentity.generation + 1 : scopeIdentity.generation,
    };
    setScopeIdentity(identity);
  }
  return <ComposerReferenceTransferBoundary key={`composer-scope-${identity.generation}`} scope={props.draftPersistenceScope ?? null} consumeReferenceTransient={referenceInputEnabled} allowHandoffAcceptance={referenceInputEnabled && !props.readOnly && !props.recoveryLocked}>
    {(locked, transferNotice) => <V2ComposerScope
      {...props}
      transferNotice={transferNotice}
      referenceInputEnabled={referenceInputEnabled}
      recoveryLocked={props.recoveryLocked || locked}
      draftTransferLocked={locked}
      prefillText={locked ? null : props.prefillText}
      prefillInputDocument={locked ? null : props.prefillInputDocument}
      prefillAttachments={locked ? null : props.prefillAttachments}
    />}
  </ComposerReferenceTransferBoundary>;
}

function V2ComposerScope({
  referenceInputEnabled = false,
  onInputInteraction,
  onSend, onResolveDraftSubmissionProtocol, onCaptureSubmissionWireSnapshot, disabled, placeholder, sessionTexts, running, onCancel, prefillText, prefillInputDocument, onPrefillConsumed, onPrefillInputDocumentConsumed, statusMessage, prefillAttachments, onPrefillAttachmentsConsumed, onSteer,
  lastUserText, lastUserAttachments = [], onReplay, readOnly, recoveryLocked = false, draftPersistenceScope = null, onPendingActionHydrated, draftReconcileEpoch = 0, draftPersistenceFailureMode = "block", autoFocus, hidden, surface = "workbench", stopRequested = false, draftControlRef,
  fileDropTargetRef, sendBlockedReason = null, draftTransferLocked, transferNotice = null, submitRequest,
}: Props & { draftTransferLocked: boolean; transferNotice?: ComposerNoticeItem | null }) {
  const t = useTranslations("composer");
  const tDrive = useTranslations("google_drive");
  const filesT = useTranslations("files_page");
  const tMemoryCard = useTranslations("memory_card");
  const tAttachment = useTranslations("attachment");
  const tChatCard = useTranslations("chat_card");
  const locale = useLocale();
  const authSession = useOptionalAuthSession();
  const composerCapabilities = useComposerCapabilities();
  const draftScopeSignature = composerDraftScopeSignature(draftPersistenceScope);
  const [val, setPlainVal] = useState("");
  const inputValueRef = useRef(val);
  inputValueRef.current = val;
  const [inputDocument, setInputDocument] = useState<InputDocument | null>(null);
  const [documentRevision, setDocumentRevision] = useState<ComposerDocumentRevision | undefined>();
  const documentRevisionRef = useRef<ComposerDocumentRevision | undefined>(undefined);
  const [revisionStagePending, setRevisionStagePending] = useState(false);
  const revisionStagePendingRef = useRef(false);
  const stageDocumentRevisionRef = useRef<((request: DocumentRevisionStageRequest) => Promise<DocumentRevisionStageResult>) | undefined>(undefined);
  const revisionStageControlsRef = useRef({ readOnly, running, recoveryLocked, onCaptureSubmissionWireSnapshot });
  revisionStageControlsRef.current = { readOnly, running, recoveryLocked, onCaptureSubmissionWireSnapshot };
  const inputDocumentRef = useRef<InputDocument | null>(null);
  const [inputResetKey, setInputResetKey] = useState(0);
  const restoreInputFocusRef = useRef(false);
  const [inputNotice, setInputNotice] = useState<string | null>(null);
  const [driveNotConnected, setDriveNotConnected] = useState(false);
  const acceptsInputDraftRef = useRef(referenceInputEnabled);
  acceptsInputDraftRef.current ||= referenceInputEnabled || inputDocument !== null;
  inputDocumentRef.current = inputDocument;
  const setVal = (next: string | ((value: string) => string), focus = false) => {
    const previous = inputValueRef.current;
    const value = typeof next === "function" ? next(previous) : next;
    const document = inputDocumentRef.current;
    if (value === previous) return;
    if (document?.nodes.some((node) => node.type === "reference") && value && !value.startsWith(previous)) {
      setInputNotice(t("reference_edit_conflict"));
      return;
    }
    let updated: InputDocument | null = null;
    try {
      updated = document && value && value.startsWith(previous)
        ? parseInputDocument({ schema_version: 1, nodes: [...document.nodes, { type: "text", text: value.slice(previous.length) }] })
        : document ? inputDocumentFromText(value) : null;
    } catch {
      setInputNotice(t("reference_edit_conflict"));
      return;
    }
    restoreInputFocusRef.current = focus || Boolean(composerRef.current?.contains(window.document.activeElement));
    inputDocumentRef.current = updated;
    inputValueRef.current = value;
    currentValueRef.current = value;
    setInputDocument(updated);
    setPlainVal(value);
    setInputResetKey((key) => key + 1);
  };
  const adoptInputDraft = (draft: InputDraftContent) => {
    acceptsInputDraftRef.current ||= Boolean(draft.documentRevision);
    const source = draft.input_document ? parseInputDocument(draft.input_document) : null;
    const document = source && (referenceInputEnabled || source.nodes.some((node) => node.type === "reference")) ? source : null;
    documentRevisionRef.current = draft.documentRevision ? copyComposerDocumentRevision(draft.documentRevision) : undefined;
    setDocumentRevision(documentRevisionRef.current);
    restoreInputFocusRef.current = Boolean(composerRef.current?.contains(window.document.activeElement));
    inputDocumentRef.current = document;
    inputValueRef.current = draft.message;
    currentValueRef.current = draft.message;
    setInputDocument(document);
    setPlainVal(draft.message);
    setInputResetKey((key) => key + 1);
  };
  const readInputDraft = async (scope: ComposerDraftScope): Promise<(ComposerOrdinaryDraft & InputDraftContent) | null> => {
    const draft = await readComposerInputDraft(scope);
    if (!draft || acceptsInputDraftRef.current || draft.documentRevision) return draft;
    if (draft.input_document?.nodes.some((node) => node.type === "reference"))
      throw new ComposerPersistenceVersionError("Reference authoring is unavailable");
    const { input_document: _document, ...textDraft } = draft;
    return textDraft;
  };
  const writeInputDraft = (scope: ComposerDraftScope, draft: InputDraftContent, revision: string | null) =>
    trackDraftOperation(acceptsInputDraftRef.current || draft.input_document || draft.documentRevision
      ? writeComposerInputDraft(scope, { input_document: draft.input_document ?? inputDocumentFromText(draft.message), attachments: draft.attachments,
          ...(draft.documentRevision ? { documentRevision: draft.documentRevision } : {}) }, revision)
      : writeComposerOrdinaryDraft(scope, draft, revision));
  const readInputTransient = (signature: string): (InputDraftContent & { expectedRevision: string | null }) | null =>
    readComposerInputTransientDraft(signature);
  const retainInputTransient = (signature: string, draft: InputDraftContent & { expectedRevision: string | null }) =>
    retainComposerInputTransientDraft(signature, inputTransient(draft));
  const clearInputTransient = (signature: string, draft: InputDraftContent & { expectedRevision: string | null }) =>
    clearComposerTransientDraft(signature, inputTransient(draft));
  const [items, setItems] = useState<AttachmentItem[]>([]);
  const itemsRef = useRef(items);
  itemsRef.current = items;
  // D10: adding a file that is already attached outlines the existing chip once and is announced, no second chip.
  const [duplicateFlashId, setDuplicateFlashId] = useState<string | null>(null);
  const duplicateFlashTimerRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (duplicateFlashTimerRef.current !== null) window.clearTimeout(duplicateFlashTimerRef.current);
  }, []);
  const [ordinaryDraftReadyFor, setOrdinaryDraftReadyFor] = useState("");
  const [draftSaveFailed, setDraftSaveFailed] = useState(false);
  const [draftReadBlocked, setDraftReadBlocked] = useState(false);
  const [draftVersionBlocked, setDraftVersionBlocked] = useState(false);
  const [referenceDraft, setReferenceDraft] = useState<ComposerReferenceDraft | null>(null);
  const [draftDiscardAllowed, setDraftDiscardAllowed] = useState(true);
  const [draftConflict, setDraftConflict] = useState(false);
  const [referenceConflict, setReferenceConflict] = useState<(ComposerOrdinaryDraft & InputDraftContent) | null>(null);
  const [draftPersistenceUnavailable, setDraftPersistenceUnavailable] = useState(false);
  const [acceptedCleanupBlocked, setAcceptedCleanupBlocked] = useState(false);
  const [acceptedCleanupRetrying, setAcceptedCleanupRetrying] = useState(false);
  const acceptedCleanupRef = useRef<AcceptedDraftCleanup | null>(null);
  const [draftDiscardPending, setDraftDiscardPending] = useState(false);
  const [draftRetryEpoch, setDraftRetryEpoch] = useState(0);
  const [completedDraftReconcileEpoch, setCompletedDraftReconcileEpoch] =
    useState(draftReconcileEpoch);
  const draftReconcilePendingRef = useRef(false);
  const ordinaryDraftRevisionRef = useRef<string | null>(null);
  const lastPersistedDraftFingerprintRef = useRef<string | null>(null);
  const ordinaryDraftWriteChainRef = useRef<Promise<void>>(Promise.resolve());
  const draftOperationsRef = useRef(new Set<Promise<unknown>>());
  const ordinaryDraftPendingWritesRef = useRef(0);
  const composerMountedRef = useRef(false);
  // Set once the user discarded this instance's draft: no later
  // autosave or unmount flush may write it back before the remount.
  const draftDiscardedForResetRef = useRef(false);
  const latestOrdinaryDraftRef = useRef<{
    scope: ComposerDraftScope;
    draft: InputDraftContent;
  } | null>(null);
  const currentValueRef = useRef("");
  const chartPrefillRef = useRef<{ text: string; fileId: string; scope: string } | null>(null);
  const currentItemsRef = useRef<AttachmentItem[]>([]);
  const currentDraftScopeRef = useRef<ComposerDraftScope | null>(null);
  const ordinaryDraftReadyForRef = useRef("");
  const onPendingActionHydratedRef = useRef(onPendingActionHydrated);
  const lastDraftReconcileEpochRef = useRef(draftReconcileEpoch);
  const draftHydrationGenerationRef = useRef(0);
  const hydratedDraftReadRef = useRef<string | null>(null);
  const draftHydrationLockedRef = useRef(recoveryLocked);
  const lockedDraftPreviewRef = useRef<string | null>(null);
  const draftReadBlockedRef = useRef(false);
  const draftConflictRef = useRef(false);
  const draftPersistenceUnavailableRef = useRef(false);
  const acceptedCleanupBlockedRef = useRef(acceptedCleanupBlocked);
  const ordinaryDraftHydrating = Boolean(
    draftPersistenceScope
    && ordinaryDraftReadyFor !== draftScopeSignature,
  );
  const draftReconcilePending = Boolean(
    draftPersistenceScope
    && draftReconcileEpoch !== completedDraftReconcileEpoch,
  );
  draftReconcilePendingRef.current = draftReconcilePending;
  const recoveryMutationLocked = revisionStagePending || recoveryLocked
    || ordinaryDraftHydrating
    || draftReconcilePending
    || draftReadBlocked
    || acceptedCleanupBlocked;
  const mutationLocked = recoveryMutationLocked || draftConflict;
  const [steerPending, setSteerPending] = useState(false);
  const [sendPending, setSendPending] = useState(false);
  const sendPendingRef = useRef(false);
  const submitRef = useRef<() => void>(() => undefined);
  const handledSubmitRequest = useRef(submitRequest ?? 0);
  useEffect(() => {
    if (submitRequest === undefined || submitRequest <= handledSubmitRequest.current) return;
    handledSubmitRequest.current = submitRequest;
    submitRef.current();
  }, [submitRequest]);
  // Latest Replay entry for the imperative control (assigned where requestReplay is defined).
  const requestReplayRef = useRef<(() => boolean) | null>(null);
  const submissionHandedOffRef = useRef(false);
  const [cancelPending, setCancelPending] = useState(false);
  const stopPending = cancelPending || stopRequested;
  const stopStatus = cancelPending
    ? t("stopping")
    : stopRequested
      ? t("stop_requested")
      : null;
  const cancelPendingRef = useRef(false);
  const compositionEpochRef = useRef(0);
  const compositionPhaseRef = useRef<"idle" | "composing" | "awaiting_final_input" | "settling">("idle");
  const compositionSettleFrameRef = useRef<number | null>(null);
  const nativeInputVersionRef = useRef(0);
  const compositionFencedRef = useRef(false);
  const [compositionFenced, setCompositionFenced] = useState(false);
  const [effectiveHidden, setEffectiveHidden] = useState(Boolean(hidden));
  const [toolsOpen, setToolsOpen] = useState(false);
  const composerRef = useRef<HTMLDivElement | null>(null);
  const composerHadFocusRef = useRef(false);
  const toolsTriggerRef = useRef<HTMLButtonElement | null>(null);
  const toolsDialogId = `composer-tools-${useId().replaceAll(":", "")}`;
  const actionTooltipId = `composer-action-${useId().replaceAll(":", "")}`;
  const layoutMode = useComposerLayoutMode(composerRef);
  const draftScopeSignatureRef = useRef(draftScopeSignature);
  draftScopeSignatureRef.current = draftScopeSignature;
  draftReadBlockedRef.current = draftReadBlocked;
  draftConflictRef.current = draftConflict;
  draftPersistenceUnavailableRef.current = draftPersistenceUnavailable;
  currentValueRef.current = val;
  currentItemsRef.current = items;
  currentDraftScopeRef.current = draftPersistenceScope;
  ordinaryDraftReadyForRef.current = ordinaryDraftReadyFor;
  acceptedCleanupBlockedRef.current = acceptedCleanupBlocked;
  onPendingActionHydratedRef.current = onPendingActionHydrated;

  useEffect(() => {
    composerMountedRef.current = true;
    return () => {
      composerMountedRef.current = false;
    };
  }, []);

  const trackDraftOperation = <T,>(operation: Promise<T>): Promise<T> => {
    draftOperationsRef.current.add(operation);
    const remove = () => { draftOperationsRef.current.delete(operation); };
    void operation.then(remove, remove);
    return operation;
  };

  const queueOrdinaryDraftWrite = (
    scope: ComposerDraftScope,
    scopeSignature: string,
    draft: InputDraftContent,
    options: { coalesceSameDraft?: boolean } = {},
  ): Promise<string | null> => {
    draft = inputDraftContent(draft);
    ordinaryDraftPendingWritesRef.current += 1;
    const operation = ordinaryDraftWriteChainRef.current
      .catch(() => undefined)
      .then(async () => {
        if (
          draftDiscardedForResetRef.current
          || draftScopeSignatureRef.current !== scopeSignature
          || draftReadBlockedRef.current
          || draftConflictRef.current
          || draftPersistenceUnavailableRef.current
          || acceptedCleanupBlockedRef.current
          || acceptedCleanupRef.current
          || draftReconcilePendingRef.current
        ) return null;
        try {
          const expectedRevision = ordinaryDraftRevisionRef.current;
          const outcome = await writeInputDraft(
            scope,
            draft,
            expectedRevision,
          );
          if (outcome.status === "conflict") {
            if (
              !composerMountedRef.current
              || draftScopeSignatureRef.current !== scopeSignature
            ) return null;
            const latest = await readInputDraft(scope);
            if (
              composerMountedRef.current
              && draftScopeSignatureRef.current === scopeSignature
            ) {
              const attemptedFingerprint = ordinaryDraftFingerprint(draft.message, draft.attachments, draft.input_document, draft.documentRevision);
              const latestFingerprint = latest
                ? ordinaryDraftFingerprint(latest.message, latest.attachments, latest.input_document, latest.documentRevision)
                : null;
              if (
                latest
                && latestFingerprint === attemptedFingerprint
                && options.coalesceSameDraft !== false
              ) {
                ordinaryDraftRevisionRef.current = latest.revision;
                lastPersistedDraftFingerprintRef.current = latestFingerprint;
                clearInputTransient(scopeSignature, {
                  ...draft,
                  expectedRevision,
                });
                draftConflictRef.current = false;
                setDraftConflict(false);
                setDraftSaveFailed(false);
                if (latest.pendingAction) {
                  const latestItems = latest.attachments.map(attachmentRefToItem);
                  currentValueRef.current = latest.message;
                  currentItemsRef.current = latestItems;
                  adoptInputDraft(latest);
                  setItems(latestItems);
                  onPendingActionHydratedRef.current?.(latest);
                }
                return latest.revision;
              }
              draftConflictRef.current = true;
              setDraftConflict(true);
              setDraftSaveFailed(true);
              latestOrdinaryDraftRef.current = null;
              if (latest) {
                latestOrdinaryDraftRef.current = {
                  scope,
                  draft: inputDraftContent(latest),
                };
              }
            }
            return null;
          }
          if (
            outcome.status !== "written"
            || (
              (draft.message || draft.attachments.length > 0)
              && typeof outcome.revision !== "string"
            )
          ) {
            throw new ComposerOrdinaryDraftInvalidError(
              "composer ordinary draft write outcome invalid",
            );
          }
          ordinaryDraftRevisionRef.current = outcome.revision;
          lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint(draft.message, draft.attachments, draft.input_document, draft.documentRevision);
          clearInputTransient(scopeSignature, {
            ...draft,
            expectedRevision,
          });
          if (
            !composerMountedRef.current
            || draftScopeSignatureRef.current !== scopeSignature
          ) return outcome.revision;
          setDraftSaveFailed(false);
          return outcome.revision;
        } catch (error) {
          if (
            !composerMountedRef.current
            || draftScopeSignatureRef.current !== scopeSignature
          ) throw error;
          setDraftSaveFailed(true);
          if (error instanceof ComposerRecoveryUnavailableError) {
            draftPersistenceUnavailableRef.current = true;
            setDraftPersistenceUnavailable(true);
          } else {
            draftReadBlockedRef.current = true;
            setDraftReadBlocked(true);
            setDraftVersionBlocked(composerPersistenceNeedsUpgrade(error));
          }
          throw error;
        }
      });
    const trackedOperation = operation.finally(() => {
      ordinaryDraftPendingWritesRef.current -= 1;
    });
    ordinaryDraftWriteChainRef.current = trackedOperation.then(
      () => undefined,
      () => undefined,
    );
    return trackedOperation;
  };
  useEffect(() => () => {
    const scope = currentDraftScopeRef.current;
    if (!scope || (draftDiscardedForResetRef.current && draftOperationsRef.current.size === 0)) return;
    const signature = draftScopeSignatureRef.current;
    const pending = [...draftOperationsRef.current, ordinaryDraftWriteChainRef.current];
    const ownsDraft = !(
      revisionStageControlsRef.current.recoveryLocked
      || acceptedCleanupBlockedRef.current
      || acceptedCleanupRef.current
      || submissionHandedOffRef.current
      || ordinaryDraftReadyForRef.current !== draftScopeSignatureRef.current
    );
    const attachments: AttachmentRef[] = currentItemsRef.current
      .filter((item) => item.status === "done" && item.fileId)
      .map(attachmentItemToRef);
    const draft = { message: currentValueRef.current, attachments, ...(inputDocumentRef.current ? { input_document: inputDocumentRef.current } : {}), ...(documentRevisionRef.current ? { documentRevision: documentRevisionRef.current } : {}) };
    // A fenced reader did not hydrate this empty editor. Keeping it as an
    // "unsaved edit" would later conflict with the preserved reference source.
    const unreadableEmpty = draftReadBlockedRef.current && !draft.message && attachments.length === 0;
    const alreadySaved = (
      lastPersistedDraftFingerprintRef.current
      === ordinaryDraftFingerprint(draft.message, draft.attachments, draft.input_document, draft.documentRevision)
      && ordinaryDraftPendingWritesRef.current === 0
    );
    const flush = ownsDraft && !unreadableEmpty && !alreadySaved;
    if (!flush && draftOperationsRef.current.size === 0 && ordinaryDraftPendingWritesRef.current === 0 && !acceptedCleanupRef.current) return;
    const expectedRevision = ordinaryDraftRevisionRef.current;
    const transient = { ...draft, expectedRevision };
    if (flush) retainInputTransient(signature, transient);
    registerComposerDraftHandoff(signature, async (retry) => {
      // Settling every operation includes Send's accepted cleanup and discard,
      // not just the currently open IndexedDB transaction.
      await Promise.allSettled(pending);
      if (acceptedCleanupRef.current) {
        if (await settleAcceptedDraftCleanup(acceptedCleanupRef.current) === "blocked") {
          throw new Error("composer accepted cleanup remains pending");
        }
        return;
      }
      if (draftDiscardedForResetRef.current) {
        clearInputTransient(signature, transient);
        return;
      }
      if (!flush || draftConflictRef.current) return;
      if (!retry && (draftReadBlockedRef.current || draftPersistenceUnavailableRef.current)) {
        throw new Error("composer final save requires retry");
      }
      const latestExpectedRevision = ordinaryDraftRevisionRef.current;
      const queuedTransient = { ...draft, expectedRevision: latestExpectedRevision };
      retainInputTransient(
        signature,
        queuedTransient,
      );
      const outcome = await writeInputDraft(
        scope,
        draft,
        latestExpectedRevision,
      );
      if (outcome.status === "written") {
        ordinaryDraftRevisionRef.current = outcome.revision;
        clearInputTransient(
          signature,
          queuedTransient,
        );
      } else if (outcome.status !== "conflict") {
        throw new Error("composer final save outcome invalid");
      }
      // A competing writer still owns its revision. Keep the transient for
      // the successor's existing conflict or pending-action recovery flow.
    });
  }, []);
  stageDocumentRevisionRef.current = (request) => trackDraftOperation((async (): Promise<DocumentRevisionStageResult> => {
    let binding: ComposerDocumentRevision;
    if (!completeComposerScope(request.scope) || !request.scope.requestedSessionId) return { status: "stale" };
    try {
      binding = copyComposerDocumentRevision({
        handoffId: request.handoffId,
        target: request.target,
        ...(request.display ? { display: request.display } : {}),
      }, request.scope.requestedSessionId);
    }
    catch { return { status: "stale" }; }
    const scope = { ...request.scope };
    const signature = composerDraftScopeSignature(scope);
    const owns = () => composerMountedRef.current && draftScopeSignatureRef.current === signature;
    if (!owns()) return { status: "stale" };
    if (readOnly || running || revisionStagePendingRef.current || typeof request.instruction !== "string")
      return { status: "blocked", reason: "unsupported_mode" };
    const wire = onCaptureSubmissionWireSnapshot?.("start", false, { ...binding.target });
    if (!wire || wire.planningMode === "planning" || wire.draftChatBinding || wire.workspaceSettingsBinding || wire.coachCanvasBinding)
      return { status: "blocked", reason: "unsupported_mode" };
    if (sendPendingRef.current || submissionHandedOffRef.current || recoveryLocked || acceptedCleanupRef.current || acceptedCleanupBlockedRef.current)
      return { status: "blocked", reason: "pending_submission" };
    if (draftSaveFailed || draftPersistenceUnavailableRef.current)
      return { status: "blocked", reason: "persistence_unavailable" };
    if (draftReadBlockedRef.current || draftConflictRef.current || draftDiscardedForResetRef.current || draftReconcilePendingRef.current || ordinaryDraftReadyForRef.current !== signature)
      return { status: "blocked", reason: "draft_not_ready" };
    revisionStagePendingRef.current = true;
    setRevisionStagePending(true);
    try {
      await ordinaryDraftWriteChainRef.current;
      if (!owns()) return { status: "stale" };
      const controls = revisionStageControlsRef.current;
      const currentWire = controls.onCaptureSubmissionWireSnapshot?.("start", false, { ...binding.target });
      if (controls.readOnly || controls.running || !currentWire || currentWire.planningMode === "planning" || currentWire.draftChatBinding || currentWire.workspaceSettingsBinding || currentWire.coachCanvasBinding)
        return { status: "blocked", reason: "unsupported_mode" };
      if (controls.recoveryLocked || sendPendingRef.current || submissionHandedOffRef.current || acceptedCleanupRef.current || acceptedCleanupBlockedRef.current)
        return { status: "blocked", reason: "pending_submission" };
      if (draftReadBlockedRef.current || draftConflictRef.current || draftPersistenceUnavailableRef.current)
        return { status: "blocked", reason: "persistence_unavailable" };
      const current = documentRevisionRef.current;
      if (current?.handoffId === binding.handoffId && sameDocumentRevisionTarget(current.target, binding.target) && ordinaryDraftRevisionRef.current)
        return { status: "applied", draftRevision: ordinaryDraftRevisionRef.current };
      if (current || currentValueRef.current || currentItemsRef.current.length || inputDocumentRef.current?.nodes.some((node) => node.type === "reference"))
        return { status: "blocked", reason: "draft_not_empty" };
      const draft: InputDraftContent = { message: request.instruction, attachments: [], input_document: inputDocumentFromText(request.instruction), documentRevision: binding };
      // Publish the complete local snapshot before awaiting storage: an unmount
      // must retain the target with the instruction in its final save.
      latestOrdinaryDraftRef.current = { scope, draft };
      adoptInputDraft(draft);
      retainInputTransient(signature, { ...draft, expectedRevision: ordinaryDraftRevisionRef.current });
      const revision = await queueOrdinaryDraftWrite(scope, signature, draft, { coalesceSameDraft: false });
      if (!owns()) return { status: "stale" };
      return revision ? { status: "applied", draftRevision: revision } : { status: "blocked", reason: "persistence_unavailable" };
    } catch {
      return owns() ? { status: "blocked", reason: "persistence_unavailable" } : { status: "stale" };
    } finally {
      revisionStagePendingRef.current = false;
      if (owns()) setRevisionStagePending(false);
    }
  })());
  useEffect(() => {
    if (!draftControlRef) return;
    const control: ComposerDraftControl = {
      stageDocumentRevision: (request) => stageDocumentRevisionRef.current!(request),
      requestReplay: () => requestReplayRef.current?.() ?? false,
      readInput: () => ({ text: currentValueRef.current, hasAttachments: itemsRef.current.length > 0, hasInputDocument: inputDocumentRef.current !== null }),
      // New work explicitly abandons the current input: await queued saves, then CAS-delete only the revision
      // known to this instance. Retain pending submissions, conflicts, unreadable drafts and newer versions from other tabs.
      discardForNewWork: () => trackDraftOperation((async () => {
        const scope = currentDraftScopeRef.current;
        if (!scope) return true;
        if (
          revisionStagePendingRef.current
          || sendPendingRef.current
          || submissionHandedOffRef.current
          || acceptedCleanupRef.current
          || acceptedCleanupBlockedRef.current
          || draftReadBlockedRef.current
          || draftConflictRef.current
          || draftPersistenceUnavailableRef.current
          || ordinaryDraftReadyForRef.current !== draftScopeSignatureRef.current
        ) return false;
        const scopeSignature = draftScopeSignatureRef.current;
        draftDiscardedForResetRef.current = true;
        try {
          await ordinaryDraftWriteChainRef.current.catch(() => undefined);
          const revision = ordinaryDraftRevisionRef.current;
          const cleared = revision
            ? await clearComposerInputDraft(scope, revision)
            : true;
          if (!cleared) {
            draftDiscardedForResetRef.current = false;
            return false;
          }
          forgetComposerTransientDraft(scopeSignature);
          return true;
        } catch {
          draftDiscardedForResetRef.current = false;
          return false;
        }
      })()),
    };
    draftControlRef.current = control;
    return () => {
      if (draftControlRef.current === control) draftControlRef.current = null;
    };
  }, [draftControlRef]);
  useComposerVisualViewport(composerRef);
  useEffect(() => {
    if (effectiveHidden) return;
    track("bowhead_composer_layout_mode_total", { surface, mode: layoutMode });
  }, [effectiveHidden, layoutMode, surface]);
  useEffect(() => {
    if (hidden && compositionFenced) return;
    setEffectiveHidden(Boolean(hidden));
  }, [compositionFenced, hidden]);
  useEffect(() => {
    if (
      layoutMode === "comfortable"
      || readOnly
      || mutationLocked
      || effectiveHidden
      || sendPending
      || steerPending
      || compositionFenced
    ) {
      setToolsOpen(false);
    }
  }, [compositionFenced, effectiveHidden, layoutMode, mutationLocked, readOnly, sendPending, steerPending]);

  // Sprint chat-v4-streaming Wave 4 — "换个问法" prefill. When prefillText
  // changes to a non-empty value, overwrite local val + focus textarea so
  // user can edit and submit. We watch `prefillText` directly; parent should
  // null/clear it after one effect cycle to avoid re-prefilling on unrelated
  // re-renders.
  useEffect(() => {
    // Feishu 07.06 #4 review[6]-fix: fill only an empty input; the composer remains editable throughout
    // steer mode, so users may have drafted their next message; unconditional overwrite on Stop would destroy it.
    if (
      prefillText
      && prefillText.trim()
      && !recoveryLocked
      && !ordinaryDraftHydrating
      && !draftReadBlocked
      && !draftConflict
    ) {
      const selectedFile = prefillAttachments?.filter((ref) => ref.selectedContext).at(-1)?.fileId;
      const previous = chartPrefillRef.current;
      const untouchedChartPrefill = selectedFile && previous?.fileId === selectedFile && previous.scope === draftScopeSignature && previous.text === currentValueRef.current;
      if (currentValueRef.current.trim() && !untouchedChartPrefill) {
        onPrefillConsumed?.("discarded");
        return;
      }
      chartPrefillRef.current = selectedFile ? { text: prefillText, fileId: selectedFile, scope: draftScopeSignature } : null;
      currentValueRef.current = prefillText;
      setVal(prefillText, true);
      onPrefillConsumed?.("applied");
      requestAnimationFrame(() => {
        focusComposerFallback(composerRef.current, textareaRef.current);
        // Move caret to end so user can append/edit.
        const el = textareaRef.current;
        if (el) el.setSelectionRange(el.value.length, el.value.length);
      });
    }
  }, [draftConflict, draftReadBlocked, onPrefillConsumed, ordinaryDraftHydrating, prefillText, prefillAttachments, draftScopeSignature, recoveryLocked]);
  useEffect(() => {
    if (!prefillInputDocument || recoveryLocked || ordinaryDraftHydrating || draftReadBlocked || draftConflict) return;
    if (!referenceInputEnabled || currentValueRef.current.trim() || inputDocumentRef.current) {
      onPrefillInputDocumentConsumed?.("discarded");
      return;
    }
    let document: InputDocument;
    try {
      document = parseInputDocument(prefillInputDocument);
    } catch {
      onPrefillInputDocumentConsumed?.("discarded");
      return;
    }
    const message = inputDocumentDisplayText(document);
    inputDocumentRef.current = document;
    inputValueRef.current = message;
    currentValueRef.current = message;
    setInputDocument(document);
    setPlainVal(message);
    setInputResetKey((key) => key + 1);
    onPrefillInputDocumentConsumed?.("applied");
    requestAnimationFrame(() => focusComposerFallback(composerRef.current, textareaRef.current));
  }, [draftConflict, draftReadBlocked, onPrefillInputDocumentConsumed, ordinaryDraftHydrating, prefillInputDocument, recoveryLocked, referenceInputEnabled]);
  const previousDraftScopeRef = useRef(draftScopeSignature);
  useEffect(() => {
    // A submission recovery lock permits reads; the predecessor handoff
    // barrier does not. Its final save must settle before this owner reads.
    if (draftTransferLocked) return;
    const lockChanged = draftHydrationLockedRef.current !== recoveryLocked;
    draftHydrationLockedRef.current = recoveryLocked;
    // Locking an already hydrated editor must not rebase an unsaved edit onto
    // another writer's revision. A fresh locked mount may read a preview only.
    if (lockChanged && ordinaryDraftReadyForRef.current === draftScopeSignature && lockedDraftPreviewRef.current === null) return;
    const hydrationKey = JSON.stringify([draftScopeSignature, draftRetryEpoch]);
    // Prop identity, capability and persistence-policy refreshes are not a
    // user decision to merge. Keep the revision paired with the local edit;
    // adopting only a newer revision would silently overwrite another tab.
    if (hydratedDraftReadRef.current === hydrationKey
      && ordinaryDraftReadyForRef.current === draftScopeSignature
      && lockedDraftPreviewRef.current === null) return;
    let current = true;
    const hydrationGeneration = ++draftHydrationGenerationRef.current;
    const ownsHydration = () => (
      current
      && draftHydrationGenerationRef.current === hydrationGeneration
    );
    if (previousDraftScopeRef.current !== draftScopeSignature) {
      // Text typed before the first scope resolved has no other home, so it
      // carries into that scope; leaving a scope always starts empty.
      const leavingScope = Boolean(previousDraftScopeRef.current);
      previousDraftScopeRef.current = draftScopeSignature;
      if (leavingScope) {
        lockedDraftPreviewRef.current = null;
        adoptInputDraft({ message: "", attachments: [] });
        setItems([]);
      }
    }
    ordinaryDraftRevisionRef.current = null;
    lastPersistedDraftFingerprintRef.current = null;
    draftReadBlockedRef.current = false;
    draftConflictRef.current = false;
    draftPersistenceUnavailableRef.current = false;
    setDraftReadBlocked(false);
    setDraftVersionBlocked(false);
    setDraftDiscardAllowed(true);
    setDraftConflict(false);
    setDraftPersistenceUnavailable(false);
    if (!acceptedCleanupRef.current) setDraftSaveFailed(false);
    setOrdinaryDraftReadyFor("");
    if (!draftPersistenceScope) return;
    const acceptedCleanup = acceptedCleanupRef.current;
    if (acceptedCleanup) {
      const cleanupItems = acceptedCleanup.transient.attachments.map(
        attachmentRefToItem,
      );
      ordinaryDraftRevisionRef.current = acceptedCleanup.revision;
      lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint(acceptedCleanup.transient.message, acceptedCleanup.transient.attachments, acceptedCleanup.transient.input_document, acceptedCleanup.transient.documentRevision);
      currentValueRef.current = acceptedCleanup.transient.message;
      currentItemsRef.current = cleanupItems;
      adoptInputDraft(acceptedCleanup.transient);
      setItems(cleanupItems);
      setOrdinaryDraftReadyFor(draftScopeSignature);
      return;
    }
    const transientDraft = readInputTransient(draftScopeSignature);
    if (transientDraft) {
      lockedDraftPreviewRef.current = null;
      currentValueRef.current = transientDraft.message;
      currentItemsRef.current = transientDraft.attachments.map(attachmentRefToItem);
      adoptInputDraft(transientDraft);
      setItems(currentItemsRef.current);
    }
    void readInputDraft(draftPersistenceScope)
      .then((draft) => {
        if (!ownsHydration()) return;
        hydratedDraftReadRef.current = hydrationKey;
        if (draft?.pendingAction) {
          if (
            transientDraft
            && ordinaryDraftFingerprint(transientDraft.message, transientDraft.attachments, transientDraft.input_document, transientDraft.documentRevision) !== ordinaryDraftFingerprint(draft.message, draft.attachments, draft.input_document, draft.documentRevision)
          ) {
            latestOrdinaryDraftRef.current = {
              scope: draftPersistenceScope,
              draft: inputDraftContent(draft),
            };
            draftConflictRef.current = true;
            setDraftConflict(true);
            setDraftSaveFailed(true);
            return;
          }
          ordinaryDraftRevisionRef.current = draft.revision;
          lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint(draft.message, draft.attachments, draft.input_document, draft.documentRevision);
          const markerItems = draft.attachments.map(attachmentRefToItem);
          currentValueRef.current = draft.message;
          currentItemsRef.current = markerItems;
          adoptInputDraft(draft);
          setItems(markerItems);
          lockedDraftPreviewRef.current = recoveryLocked && !transientDraft ? lastPersistedDraftFingerprintRef.current : null;
          if (transientDraft && !recoveryLocked) {
            clearInputTransient(draftScopeSignature, transientDraft);
          }
          onPendingActionHydratedRef.current?.(draft);
          return;
        }
        ordinaryDraftRevisionRef.current = draft?.revision ?? null;
        lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint(draft?.message ?? "", draft?.attachments ?? [], draft?.input_document, draft?.documentRevision);
        if (transientDraft) {
          const persistedFingerprint = ordinaryDraftFingerprint(draft?.message ?? "", draft?.attachments ?? [], draft?.input_document, draft?.documentRevision);
          const transientFingerprint = ordinaryDraftFingerprint(transientDraft.message, transientDraft.attachments, transientDraft.input_document, transientDraft.documentRevision);
          if (
            draft?.revision !== transientDraft.expectedRevision
            && persistedFingerprint !== transientFingerprint
          ) {
            latestOrdinaryDraftRef.current = draft
              ? {
                  scope: draftPersistenceScope,
                  draft: inputDraftContent(draft),
                }
              : null;
            draftConflictRef.current = true;
            setDraftConflict(true);
            setDraftSaveFailed(true);
            return;
          }
          if (persistedFingerprint === transientFingerprint && !recoveryLocked) {
            clearInputTransient(draftScopeSignature, transientDraft);
          }
          return;
        }
        if (lockedDraftPreviewRef.current !== null) {
          // A read-only preview is not a new edit. On unlock, re-read the
          // original scope so accepted cleanup or a newer draft cannot be
          // overwritten by the preview that was displayed during recovery.
          const currentFingerprint = ordinaryDraftFingerprint(currentValueRef.current,
            currentItemsRef.current.filter((item) => item.status === "done" && item.fileId).map(attachmentItemToRef),
            inputDocumentRef.current ?? undefined, documentRevisionRef.current);
          if (currentFingerprint === lockedDraftPreviewRef.current) {
            adoptInputDraft(draft ?? { message: "", attachments: [] });
            setItems(draft?.attachments.map(attachmentRefToItem) ?? []);
            lockedDraftPreviewRef.current = recoveryLocked ? lastPersistedDraftFingerprintRef.current : null;
            return;
          }
          lockedDraftPreviewRef.current = null;
        }
        if (!draft) return;
        if (recoveryLocked && !currentValueRef.current && currentItemsRef.current.length === 0 && !documentRevisionRef.current)
          lockedDraftPreviewRef.current = lastPersistedDraftFingerprintRef.current;
        if (!currentValueRef.current) adoptInputDraft(draft);
        setItems((existing) => (
          existing.length > 0
            ? existing
            : draft.attachments.map(attachmentRefToItem)
        ));
      })
      .catch((error: unknown) => {
        // A recovery-enabled send remains fail-closed. Default legacy Chat is
        // fail-open: warn, stop touching local draft storage, and keep sending.
        if (ownsHydration()) {
          console.warn(
            "composer ordinary draft read unavailable",
            error instanceof Error ? error.name : "unknown_error",
          );
          if (
            !(error instanceof ComposerRecoveryUnavailableError)
            || draftPersistenceFailureMode === "block"
          ) {
            draftReadBlockedRef.current = true;
            setDraftReadBlocked(true);
            setDraftVersionBlocked(composerPersistenceNeedsUpgrade(error));
            setDraftDiscardAllowed(
              !(error instanceof ComposerOrdinaryDraftInvalidError)
              || !error.pendingActionPresent,
            );
          } else {
            draftPersistenceUnavailableRef.current = true;
            setDraftPersistenceUnavailable(true);
          }
          setDraftSaveFailed(true);
        }
      })
      .finally(() => {
        if (ownsHydration()) setOrdinaryDraftReadyFor(draftScopeSignature);
      });
    return () => {
      current = false;
    };
  }, [draftPersistenceFailureMode, draftPersistenceScope, draftRetryEpoch, draftScopeSignature, draftTransferLocked, recoveryLocked, referenceInputEnabled]);
  useEffect(() => {
    if (
      draftReconcileEpoch === lastDraftReconcileEpochRef.current
      || !draftPersistenceScope
      || draftTransferLocked
    ) return;
    lastDraftReconcileEpochRef.current = draftReconcileEpoch;
    let current = true;
    const hydrationGeneration = ++draftHydrationGenerationRef.current;
    const ownsHydration = () => (
      current
      && draftHydrationGenerationRef.current === hydrationGeneration
    );
    setOrdinaryDraftReadyFor("");
    void readInputDraft(draftPersistenceScope)
      .then((draft) => {
        if (!ownsHydration()) return;
        ordinaryDraftRevisionRef.current = draft?.revision ?? null;
        lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint(draft?.message ?? "", draft?.attachments ?? [], draft?.input_document, draft?.documentRevision);
        const nextItems = draft?.attachments.map(attachmentRefToItem) ?? [];
        currentValueRef.current = draft?.message ?? "";
        currentItemsRef.current = nextItems;
        latestOrdinaryDraftRef.current = draft
          ? {
              scope: draftPersistenceScope,
              draft: inputDraftContent(draft),
            }
          : null;
        adoptInputDraft(draft ?? { message: "", attachments: [] });
        lockedDraftPreviewRef.current = revisionStageControlsRef.current.recoveryLocked ? lastPersistedDraftFingerprintRef.current : null;
        setItems(nextItems);
        // The editor now equals the exact persisted state, so an earlier
        // retained-send conflict has nothing left to merge. Unlocking a
        // hydrated editor no longer re-runs hydration, so clear it here.
        draftConflictRef.current = false;
        setDraftConflict(false);
        if (!acceptedCleanupRef.current) setDraftSaveFailed(false);
        if (draft?.pendingAction) {
          onPendingActionHydratedRef.current?.(draft);
        }
      })
      .catch((error: unknown) => {
        if (!ownsHydration()) return;
        setDraftSaveFailed(true);
        draftReadBlockedRef.current = true;
        setDraftReadBlocked(true);
        setDraftVersionBlocked(composerPersistenceNeedsUpgrade(error));
        setDraftDiscardAllowed(
          !(error instanceof ComposerOrdinaryDraftInvalidError)
          || !error.pendingActionPresent,
        );
      })
      .finally(() => {
        if (ownsHydration()) {
          setCompletedDraftReconcileEpoch(draftReconcileEpoch);
          setOrdinaryDraftReadyFor(draftScopeSignatureRef.current);
        }
      });
    return () => {
      current = false;
    };
  }, [draftPersistenceScope, draftReconcileEpoch, draftTransferLocked]);
  useEffect(() => {
    if (
      !draftPersistenceScope
      || ordinaryDraftReadyFor !== draftScopeSignature
      || recoveryLocked
      || draftReadBlocked
      || draftConflict
      || draftPersistenceUnavailable
      || acceptedCleanupBlocked
      || draftReconcilePending
      || sendPending
      || revisionStagePending
    ) return;
    const attachments: AttachmentRef[] = items
      .filter((item) => item.status === "done" && item.fileId)
      .map(attachmentItemToRef);
    const fingerprint = ordinaryDraftFingerprint(val, attachments, inputDocument ?? undefined, documentRevision);
    latestOrdinaryDraftRef.current = {
      scope: draftPersistenceScope,
      draft: { message: val, attachments, ...(inputDocument ? { input_document: inputDocument } : {}), ...(documentRevision ? { documentRevision } : {}) },
    };
    if (
      ordinaryDraftPendingWritesRef.current === 0
      && ordinaryDraftRevisionRef.current
      && lastPersistedDraftFingerprintRef.current === fingerprint
    ) return;
    const timer = window.setTimeout(() => {
      if (
        sendPendingRef.current
        || draftReadBlockedRef.current
        || draftConflictRef.current
        || draftPersistenceUnavailableRef.current
        || acceptedCleanupBlockedRef.current
        || acceptedCleanupRef.current
        || draftReconcilePendingRef.current
      ) return;
      void queueOrdinaryDraftWrite(draftPersistenceScope, draftScopeSignature, {
        message: val,
        attachments,
        ...(inputDocument ? { input_document: inputDocument } : {}), ...(documentRevision ? { documentRevision } : {}),
      }).catch(() => undefined);
    }, 150);
    return () => window.clearTimeout(timer);
  }, [acceptedCleanupBlocked, draftConflict, draftPersistenceScope, draftPersistenceUnavailable, draftReadBlocked, draftReconcilePending, draftScopeSignature, items, ordinaryDraftReadyFor, recoveryLocked, sendPending, val, inputDocument, documentRevision, revisionStagePending]);
  // Prefill existing artifacts: bytes are already in the store, so directly create done chips.
  // Explicit references refresh metadata and selected scope on the existing chip; manual question edits stay separate.
  useEffect(() => {
    if (!prefillAttachments || prefillAttachments.length === 0) return;
    if (recoveryLocked || ordinaryDraftHydrating || draftReadBlocked || draftConflict) return;
    const uniquePrefills = prefillAttachments.filter((attachment, index, all) =>
      all.findIndex((candidate) =>
        attachmentIdentity(candidate) === attachmentIdentity(attachment)
      ) === index
    );
    setItems((prev) => {
      const next = [...prev];
      for (const attachment of uniquePrefills) {
        const index = next.findIndex((item) => attachmentIdentity({ fileId: item.fileId ?? `local:${item.localId}`, bundleArtifactIds: item.bundleArtifactIds }) === attachmentIdentity(attachment));
        const updated = attachmentRefToItem(attachment);
        if (index < 0) next.push(updated);
        else next[index] = { ...updated, localId: next[index].localId };
      }
      requestAnimationFrame(() => focusComposerFallback(composerRef.current, textareaRef.current));
      return next;
    });
    // Explicit reference actions replace scope without adding duplicate chips.
    onPrefillAttachmentsConsumed?.(uniquePrefills);
  }, [draftConflict, draftReadBlocked, onPrefillAttachmentsConsumed, ordinaryDraftHydrating, prefillAttachments, recoveryLocked]);

  const retryOrdinaryDraftRead = () => {
    // Preserve anything the user entered while the storage read was blocked.
    // A successful retry merges persisted content into empty slots only.
    ordinaryDraftRevisionRef.current = null;
    draftReadBlockedRef.current = false;
    draftConflictRef.current = false;
    setDraftReadBlocked(false);
    setDraftVersionBlocked(false);
    setDraftConflict(false);
    setDraftSaveFailed(false);
    setOrdinaryDraftReadyFor("");
    setDraftRetryEpoch((epoch) => epoch + 1);
  };

  useEffect(() => {
    let current = true;
    setReferenceDraft(null);
    if (draftVersionBlocked && draftPersistenceScope) {
      void readComposerReferenceDraft(draftPersistenceScope)
        .then((draft) => { if (current) setReferenceDraft(draft); })
        .catch(() => { /* Unknown formats stay intact behind the version fence. */ });
    }
    return () => { current = false; };
  }, [draftVersionBlocked, draftPersistenceScope, draftRetryEpoch]);

  const removeSavedReferences = async () => {
    if (!referenceDraft || !draftPersistenceScope || readOnly || recoveryLocked) return false;
    const scopeSignature = draftScopeSignature;
    const removed = await trackDraftOperation(removeComposerDraftReferences(draftPersistenceScope, referenceDraft.revision));
    if (removed && composerMountedRef.current && draftScopeSignatureRef.current === scopeSignature) {
      setReferenceDraft(null);
      retryOrdinaryDraftRead();
    }
    return removed;
  };

  const fenceStaleDraftReads = (scopeSignature: string): boolean => {
    if (
      !composerMountedRef.current
      || draftScopeSignatureRef.current !== scopeSignature
    ) return false;
    draftHydrationGenerationRef.current += 1;
    setCompletedDraftReconcileEpoch(lastDraftReconcileEpochRef.current);
    setOrdinaryDraftReadyFor(scopeSignature);
    return true;
  };

  const settleAcceptedDraftCleanup = async (
    cleanup: AcceptedDraftCleanup,
  ): Promise<"blocked" | "settled"> => {
    const cleared = await trackDraftOperation(clearComposerInputDraft(cleanup.scope, cleanup.revision));
    const latest = cleared
      ? null
      : await readInputDraft(cleanup.scope);
    if (latest?.revision === cleanup.revision) return "blocked";

    clearInputTransient(cleanup.scopeSignature, cleanup.transient);
    if (acceptedCleanupRef.current?.revision === cleanup.revision) {
      acceptedCleanupRef.current = null;
    }
    if (!fenceStaleDraftReads(cleanup.scopeSignature)) return "settled";

    acceptedCleanupBlockedRef.current = false;
    setAcceptedCleanupBlocked(false);
    setDraftSaveFailed(false);
    if (latest) {
      const latestItems = latest.attachments.map(attachmentRefToItem);
      ordinaryDraftRevisionRef.current = latest.revision;
      lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint(latest.message, latest.attachments, latest.input_document, latest.documentRevision);
      latestOrdinaryDraftRef.current = {
        scope: cleanup.scope,
        draft: inputDraftContent(latest),
      };
      currentValueRef.current = latest.message;
      currentItemsRef.current = latestItems;
      adoptInputDraft(latest);
      setItems(latestItems);
      if (latest.pendingAction) onPendingActionHydratedRef.current?.(latest);
      return "settled";
    }

    ordinaryDraftRevisionRef.current = null;
    lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint("", []);
    latestOrdinaryDraftRef.current = null;
    const currentAttachments = currentItemsRef.current
      .filter((item) => item.status === "done" && item.fileId)
      .map(attachmentItemToRef);
    const stillOwnsSnapshot = ordinaryDraftFingerprint(
      currentValueRef.current,
      currentAttachments,
      inputDocumentRef.current ?? undefined,
      documentRevisionRef.current,
    ) === ordinaryDraftFingerprint(cleanup.transient.message, cleanup.transient.attachments, cleanup.transient.input_document, cleanup.transient.documentRevision);
    if (stillOwnsSnapshot) {
      currentValueRef.current = "";
      currentItemsRef.current = [];
      adoptInputDraft({ message: "", attachments: [] });
      setItems([]);
    }
    return "settled";
  };

  const retryAcceptedDraftCleanup = async () => {
    const cleanup = acceptedCleanupRef.current;
    if (!cleanup || acceptedCleanupRetrying) return;
    setAcceptedCleanupRetrying(true);
    try {
      await settleAcceptedDraftCleanup(cleanup);
    } catch {
      setDraftSaveFailed(true);
    } finally {
      setAcceptedCleanupRetrying(false);
    }
  };

  const mergeLatestOrdinaryDraft = async () => {
    if (!draftPersistenceScope || draftDiscardPending) return;
    const scope = draftPersistenceScope;
    const scopeSignature = draftScopeSignature;
    const ownsMergePresentation = () => (
      composerMountedRef.current
      && draftScopeSignatureRef.current === scopeSignature
    );
    setDraftDiscardPending(true);
    try {
      const latest = await readInputDraft(scope);
      if (!ownsMergePresentation()) return;
      ordinaryDraftRevisionRef.current = latest?.revision ?? null;
      lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint(latest?.message ?? "", latest?.attachments ?? [], latest?.input_document, latest?.documentRevision);
      if (!latest) {
        // The winner removed its canonical snapshot. Empty is now only the
        // remote baseline; the losing tab's different local intent remains a
        // valid unsent draft and must not be discarded.
        latestOrdinaryDraftRef.current = null;
        draftConflictRef.current = false;
        setDraftConflict(false);
        setDraftSaveFailed(false);
        setOrdinaryDraftReadyFor(draftScopeSignature);
        void queueOrdinaryDraftWrite(scope, scopeSignature, {
          message: currentValueRef.current,
          ...(inputDocumentRef.current ? { input_document: inputDocumentRef.current } : {}), ...(documentRevisionRef.current ? { documentRevision: documentRevisionRef.current } : {}),
          attachments: currentItemsRef.current
            .filter((item) => item.status === "done" && item.fileId)
            .map(attachmentItemToRef),
        }).catch(() => undefined);
        return;
      }
      if (latest.pendingAction) {
        const capturedTransient = readInputTransient(scopeSignature);
        if (capturedTransient) {
          clearInputTransient(scopeSignature, capturedTransient);
        }
        const latestItems = latest.attachments.map(attachmentRefToItem);
        currentValueRef.current = latest.message;
        currentItemsRef.current = latestItems;
        adoptInputDraft(latest);
        setItems(latestItems);
        draftConflictRef.current = false;
        setDraftConflict(false);
        setDraftSaveFailed(false);
        setOrdinaryDraftReadyFor(scopeSignature);
        onPendingActionHydratedRef.current?.(latest);
        return;
      }
      if (inputDocumentRef.current?.nodes.some((node) => node.type === "reference") || latest.input_document?.nodes.some((node) => node.type === "reference")) {
        setReferenceConflict(latest);
        return;
      }
      setVal((current) => {
        if (!latest.message.trim() || current === latest.message) return current;
        if (!current.trim()) return latest.message;
        if (current.includes(latest.message)) return current;
        if (latest.message.includes(current)) return latest.message;
        return `${current}\n\n${latest.message}`;
      });
      setItems((current) => {
        const seen = new Set(current.map((item) => attachmentIdentity({
          fileId: item.fileId ?? `local:${item.localId}`,
          bundleArtifactIds: item.bundleArtifactIds,
        })));
        const additions = latest.attachments
          .filter((attachment) => !seen.has(attachmentIdentity(attachment)))
          .map(attachmentRefToItem);
        return additions.length > 0 ? [...current, ...additions] : current;
      });
      draftConflictRef.current = false;
      setDraftConflict(false);
      setDraftSaveFailed(false);
      setOrdinaryDraftReadyFor(draftScopeSignature);
    } catch (error) {
      if (ownsMergePresentation()) {
        setDraftSaveFailed(true);
        if (composerPersistenceNeedsUpgrade(error)) {
          draftReadBlockedRef.current = true;
          setDraftReadBlocked(true);
          setDraftVersionBlocked(true);
        }
      }
    } finally {
      if (ownsMergePresentation()) setDraftDiscardPending(false);
    }
  };

  const resolveReferenceConflict = async (choice: "local" | "latest") => {
    if (!referenceConflict || !draftPersistenceScope || draftDiscardPending || readOnly || recoveryLocked || !referenceInputEnabled) return;
    const scope = draftPersistenceScope;
    const signature = draftScopeSignature;
    const shownRevision = referenceConflict.revision;
    const chosen = inputDraftContent(choice === "latest" ? referenceConflict : {
      message: currentValueRef.current,
      ...(inputDocumentRef.current ? { input_document: inputDocumentRef.current } : {}), ...(documentRevisionRef.current ? { documentRevision: documentRevisionRef.current } : {}),
      attachments: currentItemsRef.current.filter((item) => item.status === "done" && item.fileId).map(attachmentItemToRef),
    });
    const capturedTransient = readInputTransient(signature);
    const ownsChoice = () => composerMountedRef.current && draftScopeSignatureRef.current === signature;
    setDraftDiscardPending(true);
    try {
      const outcome = await writeInputDraft(scope, chosen, shownRevision);
      if (!ownsChoice()) return;
      if (outcome.status !== "written") {
        // The displayed remote revision is the only one the user chose to replace.
        setReferenceConflict(null);
        setInputNotice(t("reference_edit_conflict"));
        return;
      }
      if (capturedTransient) clearInputTransient(signature, capturedTransient);
      ordinaryDraftRevisionRef.current = outcome.revision;
      lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint(chosen.message, chosen.attachments, chosen.input_document, chosen.documentRevision);
      latestOrdinaryDraftRef.current = null;
      adoptInputDraft(chosen);
      if (choice === "latest") {
        currentItemsRef.current = chosen.attachments.map(attachmentRefToItem);
        setItems(currentItemsRef.current);
      }
      // Keeping local intent also keeps its original upload chips, including
      // uploads that settled while this CAS write was pending. Autosave uses
      // the acknowledged revision to retain any newly completed attachment.
      draftConflictRef.current = false;
      setDraftConflict(false);
      setReferenceConflict(null);
      setInputNotice(null);
      setDraftSaveFailed(false);
      setOrdinaryDraftReadyFor(signature);
    } catch (error) {
      if (ownsChoice()) {
        setDraftSaveFailed(true);
        if (composerPersistenceNeedsUpgrade(error)) {
          draftReadBlockedRef.current = true;
          setDraftReadBlocked(true);
          setDraftVersionBlocked(true);
        }
      }
    } finally {
      if (ownsChoice()) setDraftDiscardPending(false);
    }
  };

  const discardUnreadableOrdinaryDraft = async () => {
    if (!draftPersistenceScope || draftDiscardPending) return;
    const scope = draftPersistenceScope;
    const scopeSignature = draftScopeSignature;
    const ownsDiscardPresentation = () => (
      composerMountedRef.current
      && draftScopeSignatureRef.current === scopeSignature
    );
    setDraftDiscardPending(true);
    try {
      await trackDraftOperation(discardComposerOrdinaryDraft(scope));
      draftDiscardedForResetRef.current = true;
      forgetComposerTransientDraft(scopeSignature);
      if (!ownsDiscardPresentation()) return;
      draftDiscardedForResetRef.current = false;
      ordinaryDraftRevisionRef.current = null;
      lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint("", []);
      draftReadBlockedRef.current = false;
      setDraftReadBlocked(false);
      setDraftVersionBlocked(false);
      setDraftSaveFailed(false);
      setOrdinaryDraftReadyFor(draftScopeSignature);
    } catch (error) {
      if (ownsDiscardPresentation()) {
        setDraftSaveFailed(true);
        if (composerPersistenceNeedsUpgrade(error)) setDraftVersionBlocked(true);
      }
    } finally {
      if (ownsDiscardPresentation()) setDraftDiscardPending(false);
    }
  };
  // Phase 3-followup review P0-1: AbortController per chip so the × button
  // (and removeItem during in-progress states) actually aborts the in-flight
  // XHR + prevents the confirm call from running. Otherwise "Cancel" leaks
  // confirmed kb_files into the user's KB.
  const abortControllers = useRef<Map<string, AbortController>>(new Map());
  const asyncMutationBlockedRef = useRef(false);
  useEffect(() => () => {
    for (const controller of abortControllers.current.values()) controller.abort();
    abortControllers.current.clear();
    if (compositionSettleFrameRef.current !== null) {
      window.cancelAnimationFrame(compositionSettleFrameRef.current);
    }
  }, []);
  useEffect(() => {
    if (!readOnly) return;
    setToolsOpen(false);
    for (const controller of abortControllers.current.values()) controller.abort();
    abortControllers.current.clear();
  }, [readOnly]);
  // V3 PRD-B · /remember slash command status (success / parse error / api err)
  const [slashMessage, setSlashMessage] = useState<
    { kind: "ok" | "err"; text: string; error?: unknown } | null
  >(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  useTextareaAutosize(textareaRef, val, locale);
  // #88 HITL Tab completion (Claude Code style: Tab / Right@end accept, Up/Down cycle, Esc dismiss).
  const { suggestion, candidates, index, cycle, dismiss } = useAutocomplete(
    val, sessionTexts ?? [],
  );
  const inSteerMode = Boolean(running && onSteer);
  const textareaMutationLocked = Boolean(readOnly
    || mutationLocked
    || sendPending
    || steerPending);
  const controlsMutationLocked = textareaMutationLocked || compositionFenced;
  useEffect(() => {
    if (!controlsMutationLocked || !composerHadFocusRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      const root = composerRef.current;
      const active = document.activeElement;
      if (
        !root
        || (active instanceof HTMLElement
          && active !== document.body
          && !root.contains(active)
          && !active.closest(".composer-v2__model-menu"))
      ) return;
      focusComposerFallback(root, textareaRef.current, root);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [controlsMutationLocked]);
  const mutationIsBlocked = () => (
    readOnly
    || mutationLocked
    || sendPendingRef.current
    || steerPending
    || compositionFencedRef.current
  );
  const uploadDisabled = Boolean(
    (disabled && !inSteerMode) || controlsMutationLocked || !completeComposerScope(draftPersistenceScope),
  );
  // An existing upload may settle its own chip during a draft CAS conflict.
  // Send and persistence remain locked until explicit merge; recovery and
  // scope/auth cancellation still fence callbacks from older renders.
  asyncMutationBlockedRef.current = Boolean(
    readOnly || recoveryMutationLocked || sendPending || steerPending,
  );
  const uploadResponseAllowed = (signal?: AbortSignal) => (
    composerMountedRef.current && !signal?.aborted && !asyncMutationBlockedRef.current
  );

  const pickFiles = () => {
    if (uploadDisabled || mutationIsBlocked()) return;
    fileRef.current?.click();
  };

  // Google Drive selection inserts plain "name link" lines; the message stays ordinary text,
  // so live, persisted and replayed turns carry exactly what the user sent.
  const googleDriveAvailable = authSession?.me?.capabilities?.google_workspace === true;
  useEffect(() => {
    // Load Google's scripts before the click so the token popup is not blocked.
    if (googleDriveAvailable) void prepareGooglePicker().catch(() => undefined);
  }, [googleDriveAvailable]);
  const pickFromGoogleDrive = () => {
    if (controlsMutationLocked || mutationIsBlocked()) return;
    setInputNotice(null);
    // Remember the caret now: the picker takes focus away from the textarea.
    const caret = textareaRef.current?.selectionStart ?? null;
    void pickDriveFiles()
      .then((files) => {
        // The picker is async: the composer may have become read-only, sending or recovering meanwhile.
        if (files.length === 0 || !uploadResponseAllowed() || draftConflictRef.current) return;
        const text = pickedFilesAsText(files);
        setVal((previous) => insertPickedLines(previous, text, caret), true);
      })
      .catch((error: unknown) => {
        const key = googlePickerErrorKey(error);
        setInputNotice(tDrive(key));
        setDriveNotConnected(key === "not_connected");
      });
  };

  const trackAttachmentAction = (
    action: "select" | "upload" | "remove",
    result: "accepted" | "rejected" | "success" | "failed" | "cancelled",
    kind: "file" | "bundle",
    size: number,
  ) => {
    trackComposerEvent({
      event_name: "composer_attachment_action",
      action,
      result,
      kind,
      size_band: size >= PRESIGN_THRESHOLD ? "large" : "small",
      surface,
      composer_mode: layoutMode,
    });
  };

  /** Map XHR/presign error class to design's error variant taxonomy. */
  function classifyError(e: unknown): { kind: AttachmentErrorKind; message: string; cause?: unknown } {
    if (e instanceof ApiError && e.code === "too_many_attachments") {
      return { kind: "other", message: t("too_many_attachments", { max: MAX_COMPOSER_RUNTIME_ATTACHMENTS }), cause: e };
    }
    if (e instanceof ApiError && e.code?.startsWith("file_office_")) {
      return { kind: e.status === 413 ? "size" : "unsupported", message: filesT(fileError(e, "upload")), cause: e };
    }
    // Upload pipeline failures (API/object storage/disconnect/expired signature): show localized explanations, not raw
    // text like "presign expired (403 from object store)"; retain the original failure for "复制诊断".
    if (
      e instanceof ApiError
      || e instanceof UploadHttpError
      || e instanceof UploadTerminalError
      || e instanceof UploadNetworkError
      || e instanceof PresignExpiredError
      || e instanceof ConfirmExpiredError
      || e instanceof TypeError
    ) {
      return { ...classifyErrorKind(e), message: filesT(fileError(e, "upload")), cause: e };
    }
    return { ...classifyErrorKind(e), message: filesT(fileError(e, "upload")), cause: e };
  }

  function classifyErrorKind(e: unknown): { kind: AttachmentErrorKind; message: string } {
    const msg = e instanceof Error ? e.message : String(e);
    if (e instanceof PresignExpiredError || e instanceof ConfirmExpiredError) return { kind: "auth", message: msg };
    if (e instanceof UploadNetworkError) return { kind: "network", message: msg };
    if (e instanceof UploadTerminalError) {
      const status = e.cause.status;
      if (status === 413) return { kind: "size", message: msg };
      if (status === 415) return { kind: "unsupported", message: msg };
      if (status === 401 || status === 403) return { kind: "auth", message: msg };
      return { kind: "other", message: msg };
    }
    if (e instanceof UploadHttpError) {
      if (e.status === 413) return { kind: "size", message: msg };
      if (e.status === 415) return { kind: "unsupported", message: msg };
      if (e.status === 401 || e.status === 403) return { kind: "auth", message: msg };
      if (e.status >= 500) return { kind: "network", message: msg };
      return { kind: "other", message: msg };
    }
    // Other thrown errors: best-effort guess from the English text. Local pre-flight
    // validation no longer comes through here — validateUpload returns its kind directly.
    if (/too large/i.test(msg)) return { kind: "size", message: msg };
    if (/unsupported|mime/i.test(msg)) return { kind: "unsupported", message: msg };
    return { kind: "other", message: msg };
  }

  /** Drive a single file through validate → upload → status transitions. */
  const addFile = (file: File, attachmentCount: number) => {
    if (uploadDisabled || mutationIsBlocked()) return;
    const existing = currentItemsRef.current.find(
      (item) => item.status !== "error"
        && item.filename === file.name
        && item.size === file.size
        && (item.lastModified === undefined || item.lastModified === file.lastModified),
    );
    if (existing) {
      setDuplicateFlashId(existing.localId);
      if (duplicateFlashTimerRef.current !== null) window.clearTimeout(duplicateFlashTimerRef.current);
      duplicateFlashTimerRef.current = window.setTimeout(() => setDuplicateFlashId(null), DUPLICATE_FLASH_MS);
      return;
    }
    const kind: "file" | "bundle" = "file";
    const isLargeFile = file.size >= PRESIGN_THRESHOLD;
    if (attachmentCount >= UPLOAD_MAX_PER_MESSAGE) {
      trackAttachmentAction("select", "rejected", kind, file.size);
      setItems((p) => [
        ...p,
        {
          localId: mkLocalId(),
          filename: file.name,
          size: file.size,
          mime: file.type,
          status: "error",
          errorKind: "other",
          error: t("too_many_attachments", { max: UPLOAD_MAX_PER_MESSAGE }),
        },
      ]);
      return;
    }
    const issue = validateUpload(file);
    const localId = mkLocalId();
    if (issue) {
      trackAttachmentAction("select", "rejected", kind, file.size);
      setItems((p) => [
        ...p,
        {
          localId, filename: file.name, size: file.size, mime: file.type,
          status: "error", errorKind: issue.kind, error: issue.message,
        },
      ]);
      return;
    }
    // All task inputs, including archives, use file-only upload transports.
    trackAttachmentAction("select", "accepted", kind, file.size);
    setItems((p) => [
      ...p,
      {
        localId, filename: file.name, size: file.size, mime: file.type, lastModified: file.lastModified,
        kind, status: "uploading", progressPct: 0,
      },
    ]);
    // Phase 3-followup review P0-1: register an AbortController so the
    // × button (via removeItem) can actually abort the XHR.
    const ctrl = new AbortController();
    // Freeze the draft identity before any asynchronous auth, hashing or transfer.
    const uploadContext = { ...draftPersistenceScope! };
    abortControllers.current.set(localId, ctrl);
    void runUpload(file, localId, isLargeFile, uploadContext, ctrl.signal);
  };

  function runUpload(file: File, localId: string, isLargeFile: boolean, context: ComposerDraftScope, signal?: AbortSignal) {
    return (async () => {
      try {
        if (!uploadResponseAllowed(signal)) {
          abortControllers.current.delete(localId);
          return;
        }
        const resp = await uploadFile(file, {
          sessionId: context.requestedSessionId ?? undefined,
          spaceHeaders: spaceHeader(context),
          signal,
          onProgress: (pct) => {
            // Only update the chip if it still exists (user may have cancelled)
            setItems((p) =>
              uploadResponseAllowed(signal) ? p.map((it) =>
                it.localId === localId && it.status === "uploading"
                  ? { ...it, progressPct: pct }
                  : it,
              ) : p,
            );
          },
        });
        if (!uploadResponseAllowed(signal)) {
          abortControllers.current.delete(localId);
          return;
        }
        // For large files there's a brief confirming gap; we render it
        // through a synthetic 100% → confirming → done sequence.
        if (isLargeFile) {
          setItems((p) =>
            uploadResponseAllowed(signal) ? p.map((it) =>
              it.localId === localId
                ? { ...it, status: "confirming", progressPct: 100 }
                : it,
            ) : p,
          );
        }
        setItems((p) =>
          uploadResponseAllowed(signal) ? p.map((it) =>
            it.localId === localId
              ? {
                  ...it,
                  status: "done" as const,
                  progressPct: 100,
                  fileId: resp.file_id,
                  filename: resp.filename,
                  size: resp.size,
                  mime: resp.mime,
                }
              : it,
          ) : p,
        );
        if (uploadResponseAllowed(signal)) {
          trackAttachmentAction("upload", "success", "file", file.size);
        }
      } catch (e) {
        if (!uploadResponseAllowed(signal)) return;
        trackAttachmentAction("upload", "failed", "file", file.size);
        const cls = classifyError(e);
        setItems((p) =>
          uploadResponseAllowed(signal) ? p.map((it) =>
            it.localId === localId
              ? { ...it, status: "error" as const, errorKind: cls.kind, error: cls.message, errorCause: cls.cause }
              : it,
          ) : p,
        );
      } finally {
        abortControllers.current.delete(localId);
      }
    })();
  }


  /** Re-trigger upload for a chip in recoverable error state (auth/network).
   * Phase 3 review P1-1: branch on errorKind. "auth" = presign-expired or
   * sign-in needed — push to login flow (router-aware). "network" + others
   * = file picker fallback. */
  const retryItem = (localId: string) => {
    if (uploadDisabled || mutationIsBlocked()) return;
    const target = items.find((it) => it.localId === localId);
    if (!target) return;
    if (target.errorKind === "auth") {
      // /login is the existing sign-in route. window.location preserves
      // existing tab + query so user lands back here on success. Avoid
      // next/router import for now — composer doesn't use it elsewhere.
      const ret = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = appPath(`/login/?return=${ret}`);
      return;
    }
    // network / other: file picker fallback (user re-picks)
    fileRef.current?.click();
    removeItem(localId);
  };

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (uploadDisabled || mutationIsBlocked()) {
      e.target.value = "";
      return;
    }
    const files = e.target.files;
    if (!files) return;
    for (let i = 0; i < files.length; i++) addFile(files[i], items.length + i);
    // Reset input so selecting the same-named file again still triggers change.
    e.target.value = "";
  };

  const removeItem = (localId: string) => {
    if (mutationIsBlocked()) return;
    const target = items.find((item) => item.localId === localId);
    // Phase 3-followup review P0-1: abort any in-flight upload for this chip.
    // Idempotent: abort() on already-completed controller is a no-op.
    const ctrl = abortControllers.current.get(localId);
    if (ctrl) {
      ctrl.abort();
      abortControllers.current.delete(localId);
    }
    if (target) {
      const kind = target.kind === "bundle" ? "bundle" : "file";
      if (ctrl) trackAttachmentAction("upload", "cancelled", kind, target.size);
      trackAttachmentAction("remove", "accepted", kind, target.size);
    }
    setItems((p) => p.filter((it) => it.localId !== localId));
  };

  const anyUploading = items.some(
    (it) => it.status === "uploading" || it.status === "confirming" || it.status === "indexing",
  );
  const completedItems = items.filter(
    (it) => it.status === "done" && (it.fileId || (it.bundleArtifactIds?.length ?? 0) > 0),
  );
  const hasCompletedAttachments = completedItems.length > 0;

  // Workbench Spec §4: drop files into the conversation column (the input itself on the homepage without a column),
  // equivalent to clicking attachment upload (same addFile, validation and upload).
  const composerWrapRef = useRef<HTMLDivElement | null>(null);
  const fileDrop = useComposerFileDrop({
    enabled: !effectiveHidden,
    targetRef: fileDropTargetRef,
    fallbackRef: composerWrapRef,
    blocked: Boolean(readOnly || uploadDisabled || mutationLocked || sendPending || steerPending),
    currentCount: items.length,
    maxFiles: UPLOAD_MAX_PER_MESSAGE,
    onFiles: (files) => {
      files.forEach((file, index) => addFile(file, items.length + index));
    },
  });
  const dropMaxSize = `${Math.round(UPLOAD_MAX_BYTES / (1024 * 1024 * 1024))} GB`;
  const dropCopy = (verdict: ComposerDropVerdict): { title: string; hint: string } => {
    if (verdict.kind === "accept") {
      return {
        title: t("drop_accept_title"),
        hint: t("drop_accept_hint", { max: UPLOAD_MAX_PER_MESSAGE, size: dropMaxSize }),
      };
    }
    if (verdict.reason === "count") {
      return {
        title: t("drop_reject_count", { max: UPLOAD_MAX_PER_MESSAGE }),
        hint: t("drop_reject_count_hint"),
      };
    }
    if (verdict.reason === "type") {
      return { title: t("drop_reject_type"), hint: t("drop_reject_type_hint") };
    }
    return {
      title: t("drop_reject_blocked"),
      hint: readOnly ? t("drop_reject_blocked_readonly") : t("drop_reject_blocked_hint"),
    };
  };
  const dropAnnouncement = fileDrop.verdict
    ? (() => {
        const copy = dropCopy(fileDrop.verdict);
        return `${copy.title}。${copy.hint}`;
      })()
    : fileDrop.dropped
      ? fileDrop.dropped.kind === "accept"
        ? t("drop_added")
        : dropCopy(fileDrop.dropped).title
      : "";
  const dropOverlay = fileDrop.verdict && fileDrop.host
    ? createPortal(
        <div
          className="bh-file-drop"
          // Stacking, not appearance: cover controls with their own conversation-column layer (e.g. positioned model pickers at z-index 1), below all overlays.
          style={{ zIndex: 2 }}
          data-state={fileDrop.verdict.kind}
          data-testid="composer-file-drop"
          aria-hidden="true"
        >
          <div className="bh-file-drop__card">
            <span className="bh-file-drop__title">{dropCopy(fileDrop.verdict).title}</span>
            <span className="bh-file-drop__hint">{dropCopy(fileDrop.verdict).hint}</span>
          </div>
        </div>,
        fileDrop.host,
      )
    : null;

  const submit = () => {
    if (
      readOnly
      || mutationLocked
      || sendPendingRef.current
      || cancelPendingRef.current
      || stopRequested
      || compositionFencedRef.current
    ) return;
    const revisionBinding = documentRevisionRef.current ? copyComposerDocumentRevision(documentRevisionRef.current) : undefined;
    const sourceDocument = inputDocumentRef.current ? parseInputDocument(inputDocumentRef.current) : null;
    const referenceDocument = sourceDocument?.nodes.some((node) => node.type === "reference") ? sourceDocument : null;
    if (revisionBinding && (running || !draftPersistenceScope || draftPersistenceUnavailableRef.current || draftSaveFailed ||
      revisionBinding.target.session_id !== draftPersistenceScope.requestedSessionId || referenceDocument || currentItemsRef.current.length > 0)) {
      setInputNotice(t("document_revision_mode_conflict"));
      return;
    }
    const presentationText = currentValueRef.current;
    const text = referenceDocument || revisionBinding ? presentationText : presentationText.trim();
    if (referenceDocument) {
      if (!referenceInputEnabled || !draftPersistenceScope || draftPersistenceUnavailableRef.current
        || referencePlanningUnavailable) {
        setInputNotice(t("reference_entry_unavailable"));
        return;
      }
      try { validateInputDocumentTask(referenceDocument, hasCompletedAttachments); }
      catch { setInputNotice(t("reference_task_required")); return; }
    }
    if ((!text && !hasCompletedAttachments) || anyUploading || steerPending) return;
    // Bundles keep their display marker locally and expose canonical children to
    // the wire adapter. The same immutable snapshot is used by start and steer.
    const refs: AttachmentRef[] = completedItems.map((item) => attachmentItemToRef({
      ...item,
      fileId: item.fileId ?? `bundle:${item.localId}`,
    }));
    const runtimeIds = new Set(refs.flatMap((ref) => ref.bundleArtifactIds?.length ? ref.bundleArtifactIds : [ref.fileId]));
    if (runtimeIds.size > MAX_COMPOSER_RUNTIME_ATTACHMENTS) {
      setInputNotice(t("too_many_attachments", { max: MAX_COMPOSER_RUNTIME_ATTACHMENTS }));
      return;
    }
    const submissionScopeSignature = draftScopeSignature;
    const ownsSubmissionPresentation = () => (
      composerMountedRef.current
      && draftScopeSignatureRef.current === submissionScopeSignature
    );
    const baseWire = onCaptureSubmissionWireSnapshot?.(running ? "steer" : "start", Boolean(referenceDocument), revisionBinding ? { ...revisionBinding.target } : undefined);
    if (revisionBinding && (!baseWire || baseWire.planningMode === "planning" || baseWire.draftChatBinding || baseWire.workspaceSettingsBinding || baseWire.coachCanvasBinding)) {
      setInputNotice(t("document_revision_mode_conflict"));
      return;
    }
    const capturedWire = baseWire ? { ...baseWire, ...(revisionBinding ? { documentRevisionTarget: revisionBinding.target } : {}) } : undefined;
    if (onCaptureSubmissionWireSnapshot && !capturedWire) return;
    const prepareSubmission = async (
      actionKind: "start" | "steer",
    ): Promise<{
      revision: string | null;
      context: ComposerDraftSubmissionContext;
    } | null> => {
      await ordinaryDraftWriteChainRef.current;
      if (!ownsSubmissionPresentation()) return null;
      const resolvedWriteProtocol = revisionBinding ? "legacy_v0" : onResolveDraftSubmissionProtocol
        ? await onResolveDraftSubmissionProtocol(actionKind, refs, Boolean(referenceDocument))
        : "legacy_v0";
      if (!resolvedWriteProtocol || !ownsSubmissionPresentation()) return null;
      // chat-references.v1 carries its own durable submission identity and
      // exact parent fence. It must not be converted into a Terminal action.
      const writeProtocol = referenceDocument || revisionBinding ? "legacy_v0" : resolvedWriteProtocol;
      if (!draftPersistenceScope || draftPersistenceUnavailableRef.current) {
        return {
          revision: null,
          context: {
            writeProtocol,
            persistenceAvailable: false,
            draftMessage: presentationText,
            ...(capturedWire ? { wireSnapshot: capturedWire } : {}),
            ...(referenceDocument ? { referenceInputDocument: referenceDocument, referenceScopeKey: submissionScopeSignature } : {}),
          },
        };
      }
      if (writeProtocol === "terminal_v1") {
        return {
          revision: ordinaryDraftRevisionRef.current,
          context: {
            writeProtocol,
            persistenceAvailable: true,
            draftMessage: presentationText,
            ...(capturedWire ? { wireSnapshot: capturedWire } : {}),
            ...(referenceDocument ? { referenceInputDocument: referenceDocument, referenceScopeKey: submissionScopeSignature } : {}),
          },
        };
      }
      try {
        const revision = await queueOrdinaryDraftWrite(
          draftPersistenceScope,
          submissionScopeSignature,
          { message: presentationText, attachments: refs, ...(sourceDocument ? { input_document: sourceDocument } : {}), ...(revisionBinding ? { documentRevision: revisionBinding } : {}) },
          { coalesceSameDraft: false },
        );
        if (!ownsSubmissionPresentation() || revision === null) return null;
        return {
          revision,
          context: {
            writeProtocol,
            persistenceAvailable: true,
            draftMessage: presentationText,
            ...(capturedWire ? { wireSnapshot: revisionBinding ? { ...capturedWire, sourceDraftRevision: revision } : capturedWire } : {}),
            ...(referenceDocument ? { referenceInputDocument: referenceDocument, referenceScopeKey: submissionScopeSignature } : {}),
          },
        };
      } catch (error) {
        if (
          !referenceDocument && !revisionBinding
          && draftPersistenceFailureMode === "warn"
          && ownsSubmissionPresentation()
          && draftPersistenceUnavailableRef.current
        ) {
          return {
            revision: null,
            context: {
              writeProtocol,
              persistenceAvailable: false,
              draftMessage: presentationText,
              ...(capturedWire ? { wireSnapshot: capturedWire } : {}),
            ...(referenceDocument ? { referenceInputDocument: referenceDocument, referenceScopeKey: submissionScopeSignature } : {}),
            },
          };
        }
        throw error;
      }
    };
    const submittedLocalIds = new Set(completedItems.map((item) => item.localId));
    const invokePreparedSubmission = (
      actionKind: "start" | "steer",
    ): Promise<{
      prepared: {
        revision: string | null;
        context: ComposerDraftSubmissionContext;
      } | null;
      outcome: void | boolean;
    }> => {
      if (!referenceDocument && !revisionBinding && !onResolveDraftSubmissionProtocol && !draftPersistenceScope) {
        submissionHandedOffRef.current = true;
        const outcome = actionKind === "steer"
          ? onSteer!(text, refs.length ? refs : undefined)
          : onSend(text, refs.length ? refs : undefined);
        return Promise.resolve(outcome).then((settled) => ({
          prepared: null,
          outcome: settled,
        }));
      }
      return prepareSubmission(actionKind).then(async (prepared) => {
        // A denied preflight never handed a submission to the caller. Keep
        // local input without reconciling a nonexistent action or receipt.
        if (!prepared) return { prepared: null, outcome: false };
        if (draftConflictRef.current) {
          throw new ComposerRecoveryUnavailableError(
            `composer draft changed before ${actionKind}`,
          );
        }
        submissionHandedOffRef.current = true;
        const outcome = onResolveDraftSubmissionProtocol || referenceDocument || revisionBinding
          ? actionKind === "steer"
            ? onSteer!(
                text,
                refs.length ? refs : undefined,
                prepared.revision,
                prepared.context,
              )
            : onSend(
                text,
                refs.length ? refs : undefined,
                prepared.revision,
                prepared.context,
              )
          : actionKind === "steer"
            ? prepared.revision === null
              ? onSteer!(text, refs.length ? refs : undefined)
              : onSteer!(text, refs.length ? refs : undefined, prepared.revision)
            : prepared.revision === null
              ? onSend(text, refs.length ? refs : undefined)
              : onSend(text, refs.length ? refs : undefined, prepared.revision);
        return { prepared, outcome: await outcome };
      });
    };
    const finishAcceptedDraft = async (
      submittedPreparation: Promise<{
        revision: string | null;
        context: ComposerDraftSubmissionContext;
      } | null> | null,
    ): Promise<void> => {
      if (draftPersistenceScope && submittedPreparation) {
        const prepared = await submittedPreparation;
        const acceptedTransient = {
          message: presentationText,
          attachments: refs,
          expectedRevision: prepared?.revision ?? null,
          ...(sourceDocument ? { input_document: sourceDocument } : {}), ...(revisionBinding ? { documentRevision: revisionBinding } : {}),
        };
        clearInputTransient(submissionScopeSignature, acceptedTransient);
        try {
          if (prepared?.context.writeProtocol === "terminal_v1") {
            // The action settlement owns the ordinary-draft transaction. V2
            // must neither clear nor flush the accepted snapshot afterward.
            ordinaryDraftRevisionRef.current = null;
            lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint("", []);
          }
          if (
            prepared?.context.writeProtocol !== "terminal_v1"
            && prepared?.context.persistenceAvailable
            && prepared.revision
          ) {
            const revision = prepared.revision;
            const cleanup = {
              revision,
              ...acceptedTransient,
              expectedRevision: revision,
            };
            acceptedCleanupRef.current = {
              scope: draftPersistenceScope,
              scopeSignature: submissionScopeSignature,
              revision,
              transient: cleanup,
            };
            if (await settleAcceptedDraftCleanup(
              acceptedCleanupRef.current,
            ) === "blocked") {
              throw new ComposerRecoveryUnavailableError(
                "accepted composer draft cleanup did not settle",
              );
            }
            try {
              await prepared.context.finalizeReferenceSubmission?.();
            } catch {
              if (ownsSubmissionPresentation()) setDraftSaveFailed(true);
            }
            return;
          }
          if (!ownsSubmissionPresentation()) return;
          try {
            await prepared?.context.finalizeReferenceSubmission?.();
          } catch {
            setDraftSaveFailed(true);
          }
        } catch (error) {
          clearInputTransient(submissionScopeSignature, acceptedTransient);
          if (!ownsSubmissionPresentation()) return;
          setDraftSaveFailed(true);
          if (
            prepared?.context.writeProtocol !== "terminal_v1"
            && prepared?.context.persistenceAvailable
            && prepared.revision
          ) {
            // Keep this mounted composer locked while retrying the exact CAS
            // cleanup. Cross-reload recovery belongs to Terminal actions.
            acceptedCleanupBlockedRef.current = true;
            setAcceptedCleanupBlocked(true);
            return;
          }
          draftReadBlockedRef.current = true;
          setDraftReadBlocked(true);
          setDraftVersionBlocked(composerPersistenceNeedsUpgrade(error));
          setDraftDiscardAllowed(
            !(error instanceof ComposerOrdinaryDraftInvalidError)
            || !error.pendingActionPresent,
          );
          return;
        }
      }
      if (!fenceStaleDraftReads(submissionScopeSignature)) return;
      latestOrdinaryDraftRef.current = null;
      setVal((current) => {
        const matches = referenceDocument
          ? ordinaryDraftFingerprint(current, refs, inputDocumentRef.current ?? undefined, documentRevisionRef.current) === ordinaryDraftFingerprint(presentationText, refs, sourceDocument ?? undefined, revisionBinding)
          : current.trim() === text;
        const next = matches ? "" : current;
        currentValueRef.current = next;
        return next;
      });
      setItems((current) => {
        const next = current.filter((item) => !submittedLocalIds.has(item.localId));
        currentItemsRef.current = next;
        return next;
      });
    };
    const reconcileRetainedDraft = async (): Promise<void> => {
      if (!draftPersistenceScope) return;
      try {
        const latest = await readInputDraft(draftPersistenceScope);
        if (!ownsSubmissionPresentation()) return;
        if (!latest) {
          draftConflictRef.current = true;
          setDraftConflict(true);
          setDraftSaveFailed(true);
          return;
        }
        if (latest.pendingAction) {
          if (
            ordinaryDraftFingerprint(latest.message, latest.attachments, latest.input_document, latest.documentRevision)
            !== ordinaryDraftFingerprint(presentationText, refs, sourceDocument ?? undefined, revisionBinding)
          ) {
            latestOrdinaryDraftRef.current = {
              scope: draftPersistenceScope,
              draft: inputDraftContent(latest),
            };
            draftConflictRef.current = true;
            setDraftConflict(true);
            setDraftSaveFailed(true);
            return;
          }
          onPendingActionHydratedRef.current?.(latest);
          return;
        }
        if (
          ordinaryDraftFingerprint(latest.message, latest.attachments, latest.input_document, latest.documentRevision)
          !== ordinaryDraftFingerprint(presentationText, refs, sourceDocument ?? undefined, revisionBinding)
        ) {
          draftConflictRef.current = true;
          setDraftConflict(true);
          setDraftSaveFailed(true);
          return;
        }
        ordinaryDraftRevisionRef.current = latest.revision;
        lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint(latest.message, latest.attachments, latest.input_document, latest.documentRevision);
        const latestItems = latest.attachments.map(attachmentRefToItem);
        currentValueRef.current = latest.message;
        currentItemsRef.current = latestItems;
        latestOrdinaryDraftRef.current = {
          scope: draftPersistenceScope,
          draft: inputDraftContent(latest),
        };
        adoptInputDraft(latest);
        setItems(latestItems);
      } catch (error) {
        if (!ownsSubmissionPresentation()) return;
        setDraftSaveFailed(true);
        if (
          draftPersistenceFailureMode === "warn"
          && error instanceof ComposerRecoveryUnavailableError
        ) {
          draftPersistenceUnavailableRef.current = true;
          setDraftPersistenceUnavailable(true);
        } else {
          draftReadBlockedRef.current = true;
          setDraftReadBlocked(true);
          setDraftVersionBlocked(composerPersistenceNeedsUpgrade(error));
          setDraftDiscardAllowed(
            !(error instanceof ComposerOrdinaryDraftInvalidError)
            || !error.pendingActionPresent,
          );
        }
      }
    };
    // Phase 1.4 Steer — when running + onSteer provided, fire the steer path
    // instead of starting a new turn. Skip the slash-command short-circuit
    // (slash commands don't make sense mid-turn). Composer stays input-
    // enabled so users can keep nudging.
    if (inSteerMode) {
      trackComposerEvent({
        event_name: "composer_send",
        action: "steer",
        surface,
        composer_mode: layoutMode,
      });
      setSteerPending(true);
      submissionHandedOffRef.current = false;
      const steerResult = invokePreparedSubmission("steer");
      void trackDraftOperation(steerResult
        .then(async ({ prepared, outcome }) => {
          if (outcome !== false) await finishAcceptedDraft(
            prepared ? Promise.resolve(prepared) : null,
          );
          else if (prepared) await reconcileRetainedDraft();
        })
        .catch(() => {
          // Keep the user's text in place when steer delivery fails.
          setDraftSaveFailed(true);
        })
        .finally(() => {
          submissionHandedOffRef.current = false;
          setSteerPending(false);
        }));
      return;
    }
    if (disabled) return;

    const knownSlash = /^\/remember(?:\s|$)/.test(text);
    if (knownSlash) {
      if (refs.length > 0) {
        setSlashMessage({ kind: "err", text: t("slash_attachments_unsupported") });
        return;
      }
      sendPendingRef.current = true;
      submissionHandedOffRef.current = true;
      setSendPending(true);
      void trackDraftOperation((async () => {
        await ordinaryDraftWriteChainRef.current;
        let revision = ordinaryDraftRevisionRef.current;
        if (draftPersistenceScope && !draftPersistenceUnavailableRef.current) {
          revision = await queueOrdinaryDraftWrite(
            draftPersistenceScope,
            submissionScopeSignature,
            { message: presentationText, attachments: refs },
            { coalesceSameDraft: false },
          );
          if (!revision) {
            throw new ComposerRecoveryUnavailableError(
              "slash command draft fence unavailable",
            );
          }
        }
        // Show only localized guidance (syntax and correction); status codes and request IDs go in "复制诊断".
        const result = await dispatchRememberLine(text).then((remembered) => (
          remembered.ok
            ? { handled: true, ok: true, message: t("remember_saved", { key: remembered.key }), error: undefined }
            : {
                handled: true,
                ok: false,
                // Policy refusals give specific reasons (same copy as memory candidate cards); otherwise offer remedies by failure kind.
                message: remembered.refusal
                  ? tMemoryCard(`refused_${remembered.refusal}`)
                  : t(`remember_failed_${remembered.reason}`),
                error: remembered.error,
              }
        ));
        if (!result.ok) {
          setSlashMessage({ kind: "err", text: result.message, error: result.error });
          return false;
        }
        if (draftPersistenceScope && revision) {
          const cleanup = {
            revision,
            message: presentationText,
            attachments: refs,
            expectedRevision: revision,
          };
          acceptedCleanupRef.current = {
            scope: draftPersistenceScope,
            scopeSignature: submissionScopeSignature,
            revision,
            transient: cleanup,
          };
          if (await settleAcceptedDraftCleanup(
            acceptedCleanupRef.current,
          ) === "blocked") {
            acceptedCleanupBlockedRef.current = true;
            setAcceptedCleanupBlocked(true);
            setDraftSaveFailed(true);
            return false;
          }
        }
        clearInputTransient(submissionScopeSignature, {
          message: presentationText,
          attachments: refs,
          expectedRevision: revision,
        });
        if (!draftPersistenceScope || !revision) {
          ordinaryDraftRevisionRef.current = null;
          lastPersistedDraftFingerprintRef.current = ordinaryDraftFingerprint("", []);
          latestOrdinaryDraftRef.current = null;
          currentValueRef.current = "";
          currentItemsRef.current = [];
          setVal("");
          setItems([]);
        }
        setSlashMessage({ kind: "ok", text: result.message });
        return true;
      })()
        .catch(() => {
          setDraftSaveFailed(true);
          if (acceptedCleanupRef.current) {
            acceptedCleanupBlockedRef.current = true;
            setAcceptedCleanupBlocked(true);
          }
        })
        .finally(() => {
          sendPendingRef.current = false;
          submissionHandedOffRef.current = false;
          setSendPending(false);
        }));
      return;
    }

    // Sprint file-upload-presign Phase 3: bundles have NO fileId (they are
    // umbrellas for N children) — they pass through with bundleArtifactIds
    // populated so useTurnRunner can expand them into the flat attachments[]
    // sent to /chat/send. Single-file chips keep the legacy fileId path.
    sendPendingRef.current = true;
    submissionHandedOffRef.current = false;
    setSendPending(true);
    const finishSubmission = () => {
      sendPendingRef.current = false;
      submissionHandedOffRef.current = false;
      setSendPending(false);
    };
    try {
      trackComposerEvent({
        event_name: "composer_send",
        action: "start",
        surface,
        composer_mode: layoutMode,
      });
      const sendResult = invokePreparedSubmission("start");
      void trackDraftOperation(sendResult
        .then(async ({ prepared, outcome }) => {
          if (outcome !== false) await finishAcceptedDraft(
            prepared ? Promise.resolve(prepared) : null,
          );
          else if (prepared) await reconcileRetainedDraft();
        })
        .catch(() => {
          // Outcome remains unknown/rejected: keep the exact draft snapshot.
          setDraftSaveFailed(true);
        })
        .finally(finishSubmission));
    } catch {
      // Synchronous adapter failure also preserves the draft.
      finishSubmission();
    }
  };
  submitRef.current = submit;

  const [planningMode] = usePlanningModePref();
  const referencePlanningUnavailable = composerCapabilities.planning && planningMode === "planning"
    && authSession?.me?.capabilities?.composer?.reference_plan !== true;
  const showReferenceEditor = Boolean(draftPersistenceScope && referenceInputEnabled || inputDocument?.nodes.some((node) => node.type === "reference"));
  const hasInputReferences = Boolean(inputDocument?.nodes.some((node) => node.type === "reference"));
  const hasTaskText = hasInputReferences ? Boolean(inputDocumentTextContent(inputDocument!).trim()) : Boolean(val.trim());
  const referenceEntryBlocked = hasInputReferences && (!referenceInputEnabled || referencePlanningUnavailable);
  const revisionEntryBlocked = Boolean(documentRevision && (draftSaveFailed || draftPersistenceUnavailable || running || items.length > 0 || documentRevision.target.session_id !== draftPersistenceScope?.requestedSessionId));
  const sendDisabled = revisionEntryBlocked || referenceEntryBlocked || readOnly || mutationLocked || stopPending || (inSteerMode
    ? (!hasTaskText && !hasCompletedAttachments) || anyUploading || steerPending || compositionFenced
    : disabled
      || sendPending
      || compositionFenced
      || (!hasTaskText && !hasCompletedAttachments)
      || anyUploading);

  // Workbench Spec §2: persistently show why send is unavailable below the input, linked
  // to the send button via aria-describedby; Tooltip says only "发送". Include actionable persistent reasons: uploading attachments,
  // a running turn with no available "停止" (homepage), and parent-provided reasons (e.g. no usable Space). The toolbar's
  // "只读模式" label explains read-only accounts; omit fleeting IME composition/draft-read states.
  const sendBlockedNoteId = `composer-send-blocked-${useId().replaceAll(":", "")}`;
  const sendButtonShown = !sendPending && !(running && onCancel) && !readOnly;
  const sendBlockedNote = sendButtonShown
    ? anyUploading
      ? t("send_blocked_uploading")
      : disabled && !inSteerMode
        ? sendBlockedReason ?? (running ? t("send_blocked_running") : null)
        : null
    : null;

  // D9: one notice slot below the composer (error > warning > neutral); the rest wait behind "N more notices".
  const leaveDocumentRevision = () => {
    const draft: InputDraftContent = { message: currentValueRef.current, attachments: currentItemsRef.current.filter((item) => item.status === "done" && item.fileId).map(attachmentItemToRef), input_document: inputDocumentRef.current ?? inputDocumentFromText(currentValueRef.current) };
    adoptInputDraft(draft);
    if (draftPersistenceScope) latestOrdinaryDraftRef.current = { scope: draftPersistenceScope, draft };
  };
  const removeAllAttachments = () => {
    for (const item of currentItemsRef.current) removeItem(item.localId);
  };
  const noticeBaseId = `composer-notice-${useId().replaceAll(":", "")}`;
  // Fix for "too many attachments": drop the newest attachments until the message is within the limit.
  const runtimeAttachmentCount = (list: AttachmentItem[]) => new Set(
    list
      .filter((item) => item.status === "done")
      .flatMap((item) => item.bundleArtifactIds?.length ? item.bundleArtifactIds : [item.fileId ?? `bundle:${item.localId}`]),
  ).size;
  const excessAttachmentItems = (() => {
    const done = items.filter((item) => item.status === "done");
    const excess: AttachmentItem[] = [];
    for (let kept = done; runtimeAttachmentCount(kept) > MAX_COMPOSER_RUNTIME_ATTACHMENTS && kept.length > 0; kept = kept.slice(0, -1)) {
      excess.push(kept[kept.length - 1]);
    }
    return excess;
  })();
  const removeExcessAttachments = () => {
    for (const item of excessAttachmentItems) removeItem(item.localId);
    setInputNotice(null);
  };
  const retryDraftSave = () => {
    if (!draftPersistenceScope || draftPersistenceUnavailableRef.current || draftReadBlockedRef.current || draftConflictRef.current) return;
    void queueOrdinaryDraftWrite(draftPersistenceScope, draftScopeSignature, {
      message: currentValueRef.current,
      attachments: currentItemsRef.current.filter((item) => item.status === "done" && item.fileId).map(attachmentItemToRef),
      ...(inputDocumentRef.current ? { input_document: inputDocumentRef.current } : {}),
      ...(documentRevisionRef.current ? { documentRevision: documentRevisionRef.current } : {}),
    }).catch(() => undefined);
  };
  const inputNoticeText = inputNotice ?? (referenceEntryBlocked ? t("reference_entry_unavailable") : null);
  const revisionModeConflict = inputNotice !== null && inputNotice === t("document_revision_mode_conflict");
  const blockingInputNotice = inputNoticeText !== null && (
    revisionModeConflict
    || inputNoticeText === t("reference_entry_unavailable")
    || inputNoticeText === t("reference_task_required")
    || inputNoticeText === t("reference_edit_conflict")
    || inputNoticeText === t("too_many_attachments", { max: MAX_COMPOSER_RUNTIME_ATTACHMENTS })
  );
  const composerNotices: ComposerNoticeItem[] = [];
  if (transferNotice) composerNotices.push(transferNotice);
  if (draftConflict && !draftVersionBlocked) {
    composerNotices.push({
      id: "draft-conflict",
      tone: "warning",
      text: t("draft_conflict"),
      textId: `${noticeBaseId}-draft-conflict`,
      actions: [
        {
          label: draftDiscardPending ? t("draft_merging") : referenceConflict ? t("reference_keep_local") : t("draft_merge_latest"),
          onSelect: () => void (referenceConflict ? resolveReferenceConflict("local") : mergeLatestOrdinaryDraft()),
          disabled: draftDiscardPending || Boolean(referenceConflict && (readOnly || recoveryLocked || !referenceInputEnabled)),
        },
        ...(referenceConflict
          ? [{
              label: t("reference_use_latest"),
              onSelect: () => void resolveReferenceConflict("latest"),
              disabled: draftDiscardPending || readOnly || recoveryLocked || !referenceInputEnabled,
            }]
          : []),
      ],
    });
  } else if (draftReadBlocked) {
    composerNotices.push({
      id: "draft-read-blocked",
      tone: "error",
      text: t(
        draftVersionBlocked
          ? referenceDraft ? "reference_saved_locked" : "draft_version_blocked"
          : draftDiscardAllowed
            ? "draft_read_blocked"
            : "draft_marker_read_blocked",
      ),
      textId: `${noticeBaseId}-draft-read-blocked`,
      actions: [
        { label: t("draft_retry"), onSelect: retryOrdinaryDraftRead, disabled: draftDiscardPending },
        ...(draftDiscardAllowed && !draftVersionBlocked
          ? [{
              label: draftDiscardPending ? t("draft_discarding") : t("draft_discard"),
              onSelect: () => void discardUnreadableOrdinaryDraft(),
              disabled: draftDiscardPending,
            }]
          : []),
      ],
    });
  } else if (draftSaveFailed) {
    composerNotices.push(
      acceptedCleanupBlocked
        ? {
            id: "draft-cleanup",
            tone: "error",
            text: t("draft_cleanup_failed"),
            textId: `${noticeBaseId}-draft-cleanup`,
            actions: [{
              label: t(acceptedCleanupRetrying ? "draft_cleanup_retrying" : "draft_cleanup_retry"),
              onSelect: () => void retryAcceptedDraftCleanup(),
              disabled: acceptedCleanupRetrying,
            }],
          }
        : {
            id: "draft-save",
            tone: "neutral",
            text: t("draft_save_failed"),
            textId: `${noticeBaseId}-draft-save`,
            // Only when the store can still be written: an unavailable store is not fixed by trying again.
            actions: draftPersistenceUnavailable || !draftPersistenceScope
              ? undefined
              : [{ label: t("draft_save_retry"), onSelect: retryDraftSave, disabled: mutationLocked }],
          },
    );
  }
  const tooManyNotice = inputNoticeText === t("too_many_attachments", { max: MAX_COMPOSER_RUNTIME_ATTACHMENTS });
  const driveNotConnectedNotice = driveNotConnected && inputNoticeText === tDrive("not_connected");
  if (inputNoticeText !== null) {
    composerNotices.push({
      id: "input",
      tone: blockingInputNotice ? "warning" : "neutral",
      text: inputNoticeText,
      textId: `${noticeBaseId}-input`,
      actions: revisionModeConflict
        ? [
            { label: t("notice_remove_attachments"), onSelect: removeAllAttachments, disabled: mutationLocked },
            { label: t("notice_send_as_new_request"), onSelect: () => { leaveDocumentRevision(); setInputNotice(null); }, disabled: readOnly || mutationLocked || sendPending || running },
          ]
        : tooManyNotice && excessAttachmentItems.length > 0
          ? [{ label: t("notice_remove_extra", { count: excessAttachmentItems.length }), onSelect: removeExcessAttachments, disabled: mutationLocked }]
          : driveNotConnectedNotice
            ? [{ label: t("notice_connect_drive"), onSelect: () => { window.location.assign(appPath("/connections/")); } }]
            : undefined,
    });
  }
  // A failed upload: the chip keeps its name and a short state; the full sentence and diagnostics live here.
  for (const failed of items.filter((item) => item.status === "error" && item.error)) {
    const diagnostics = failed.errorCause === undefined ? undefined : errorDiagnosticsFrom(failed.errorCause);
    composerNotices.push({
      id: `upload-${failed.localId}`,
      // Neutral: the file can be removed or re-picked; a failed upload neither blocks sending nor loses the draft.
      tone: "neutral",
      text: t("upload_failed_notice", { name: failed.filename, reason: failed.error ?? "" }),
      textId: `${noticeBaseId}-upload-${failed.localId}`,
      testId: "composer-upload-failed",
      actions: diagnostics
        ? [{
            label: tChatCard("copy_diagnostics"),
            onSelect: () => {
              void copyDiagnostics(diagnostics, "upload", tChatCard("copy_fallback_prompt")).then((ok) => {
                if (ok) toast({ kind: "success", text: tChatCard("copied") });
              });
            },
          }]
        : undefined,
    });
  }
  if (statusMessage) {
    composerNotices.push({ id: "status", tone: "neutral", text: statusMessage, textId: `${noticeBaseId}-status`, testId: "composer-state-note" });
  }
  if (sendBlockedNote) {
    composerNotices.push({ id: "send-blocked", tone: "neutral", text: sendBlockedNote, textId: sendBlockedNoteId, testId: "composer-send-blocked" });
  }
  const shownNotice = orderComposerNotices(composerNotices)[0];

  const acceptSuggestion = () => {
    if (suggestion && !mutationIsBlocked()) {
      setVal((v) => v + suggestion);
    }
  };

  const requestCancel = () => {
    if (!onCancel || cancelPendingRef.current || stopRequested) return;
    cancelPendingRef.current = true;
    setCancelPending(true);
    const finish = () => {
      cancelPendingRef.current = false;
      setCancelPending(false);
    };
    try {
      const result = onCancel();
      if (result && typeof (result as Promise<void | boolean>).then === "function") {
        void (result as Promise<void | boolean>)
          .catch(() => undefined)
          .finally(finish);
      } else {
        finish();
      }
    } catch {
      finish();
    }
  };
  const requestReplay = (): boolean => {
    if (mutationIsBlocked() || sendPendingRef.current) return false;
    sendPendingRef.current = true;
    setSendPending(true);
    trackComposerEvent({
      event_name: "composer_send",
      action: "replay",
      surface,
      composer_mode: layoutMode,
    });
    const finish = () => {
      sendPendingRef.current = false;
      setSendPending(false);
    };
    try {
      const result = onReplay
        ? onReplay()
        : lastUserAttachments.length
          ? onSend(lastUserText ?? "", lastUserAttachments)
          : onSend(lastUserText ?? "");
      if (result && typeof (result as Promise<void | boolean>).then === "function") {
        void (result as Promise<void | boolean>)
          .catch(() => undefined)
          .finally(finish);
      } else {
        finish();
      }
    } catch {
      finish();
    }
    return true;
  };
  requestReplayRef.current = requestReplay;
  const primaryAction = sendPending ? (
    <button
      type="button"
      className="v2-send-btn"
      disabled
      aria-busy="true"
      aria-label={t("preparing")}
      aria-describedby={actionTooltipId}
    >
      <span className="composer-v2__send-spinner" aria-hidden="true" />
    </button>
  ) : running && onCancel ? (
    <button
      type="button"
      className="v2-send-btn v2-send-btn--cancel"
      disabled={stopPending}
      onClick={requestCancel}
      title={stopStatus ?? t("stop_title")}
      aria-label={stopStatus ?? t("stop_label")}
      aria-busy={stopPending}
      aria-describedby={actionTooltipId}
    >
      {stopPending ? <span aria-hidden="true">…</span> : <ComposerGlyph kind="stop" />}
      <span>{stopStatus ?? t("stop_label")}</span>
    </button>
  ) : (
      <button
        type="button"
        className="v2-send-btn"
        disabled={sendDisabled}
        onClick={submit}
        title={t("send_title")}
        aria-label={t("send_btn")}
        aria-describedby={sendDisabled && shownNotice?.textId ? `${actionTooltipId} ${shownNotice.textId}` : actionTooltipId}
      >
        <ComposerGlyph kind="send" />
      </button>
    );
  const primaryActionTooltip = sendPending
    ? t("preparing")
    : running && onCancel
    ? stopStatus ?? t("stop_title")
    : t("send_title");

  return (
    <div ref={composerWrapRef} onInputCapture={onInputInteraction} className="v2-composer-wrap" hidden={effectiveHidden} style={effectiveHidden ? { display: "none" } : undefined}>
      {dropOverlay}
      <span
        className="composer-v2__sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        data-testid="composer-drop-status"
      >
        {dropAnnouncement}
      </span>
      <span
        className="composer-v2__sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        data-testid="composer-action-status"
      >
        {stopStatus
          ? stopStatus
          : sendPending || steerPending
            ? t("preparing")
            : ""}
      </span>
      {slashMessage && (
        <div
          className={
            slashMessage.kind === "ok"
              ? "composer-slash-msg ok"
              : "composer-slash-msg err"
          }
          data-testid="composer-slash-msg"
          role="status"
          onClick={() => setSlashMessage(null)}
        >
          {slashMessage.text}
        </div>
      )}
      {slashMessage?.kind === "err" && slashMessage.error !== undefined ? (
        <ErrorDiagnosticsInline error={slashMessage.error} surface="chat" />
      ) : null}
      <div
        ref={composerRef}
        tabIndex={-1}
        className={`v2-composer composer-v2${sendPending ? " is-preparing" : ""}${stopPending ? " is-stopping" : ""}`}
        data-composer-layout-v2=""
        data-layout-mode={layoutMode}
        data-composer-surface={surface}
        aria-busy={sendPending || stopPending}
        onFocusCapture={() => {
          composerHadFocusRef.current = true;
        }}
        onBlurCapture={(event) => {
          const next = event.relatedTarget;
          if (
            next instanceof Node
            && !event.currentTarget.contains(next)
            && !(next instanceof HTMLElement && next.closest(".composer-v2__model-menu"))
          ) {
            composerHadFocusRef.current = false;
          }
        }}
      >
        {items.length ? (
          <div className="v2-composer-attachments">
            <span
              className="composer-v2__sr-only"
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              {duplicateFlashId
                ? tAttachment("duplicate")
                : items.some((item) => item.status === "error")
                  ? t("upload_status_failed")
                  : items.some((item) => item.status !== "done")
                    ? t("upload_status_busy")
                    : t("upload_status_ready")}
            </span>
            {items.map((it) => (
              <AttachmentChip
                key={it.localId}
                item={it}
                mutationDisabled={controlsMutationLocked}
                onRemove={removeItem}
                onCancel={removeItem}
                onRetry={retryItem}
                flash={duplicateFlashId === it.localId}
              />
            ))}
          </div>
        ) : null}

        {items.some((item) => item.status === "done" && item.kind !== "bundle" && item.filename.toLowerCase().endsWith(".zip")) ? (
          <p role="status" className="px-3 text-xs text-muted-foreground">{t("archive_file_only")}</p>
        ) : null}

        {documentRevision ? (
          <RevisionTargetBar
            revision={documentRevision}
            removeDisabled={Boolean(readOnly || mutationLocked || sendPending || running)}
            onRemove={leaveDocumentRevision}
          />
        ) : null}
        {referenceDraft ? (
          <ComposerReferencePreview
            document={referenceDraft.input_document}
            attachments={referenceDraft.attachments}
            onRemove={!readOnly && !recoveryLocked && !referenceDraft.pendingAction
              ? removeSavedReferences : undefined}
          />
        ) : null}

        {referenceConflict && draftConflict && !draftVersionBlocked ? <ComposerReferencePreview
          title={t("reference_conflict_title")}
          document={referenceConflict.input_document ?? inputDocumentFromText(referenceConflict.message)}
          attachments={referenceConflict.attachments}
        /> : null}
        <div style={{ position: "relative" }}>
          {showReferenceEditor ? (
            <ReferenceEditor
              scope={draftPersistenceScope}
              snapshot={draftPersistenceScope ? { scope: draftPersistenceScope, resetKey: String(inputResetKey), document: inputDocument ?? { schema_version: 1, nodes: [{ type: "text", text: val }] } } : null}
              readOnly={textareaMutationLocked || !referenceInputEnabled || Boolean(disabled && !inSteerMode)}
              autoFocus={autoFocus || restoreInputFocusRef.current}
              placeholder={placeholder ?? t("placeholder")}
              hasAttachments={hasCompletedAttachments}
              entryMode={inSteerMode ? "steer" : composerCapabilities.planning && planningMode === "planning" ? "plan" : "ordinary"}
              onCompositionStateChange={(blocked, scope) => {
                if (composerDraftScopeSignature(scope) !== draftScopeSignatureRef.current) return;
                compositionFencedRef.current = blocked;
                setCompositionFenced(blocked);
              }}
              onChange={(document, scope) => {
                if (textareaMutationLocked || composerDraftScopeSignature(scope) !== draftScopeSignatureRef.current) return;
                chartPrefillRef.current = null;
                const source = parseInputDocument(document);
                const message = inputDocumentDisplayText(source);
                inputDocumentRef.current = source;
                inputValueRef.current = message;
                currentValueRef.current = message;
                setInputDocument(source);
                setPlainVal(message);
                setInputNotice(null);
              }}
              onSubmitIntent={(document, scope) => {
                if (composerDraftScopeSignature(scope) !== draftScopeSignatureRef.current || JSON.stringify(document) !== JSON.stringify(inputDocumentRef.current ?? inputDocumentFromText(currentValueRef.current))) return;
                submit();
              }}
              ghostText={controlsMutationLocked ? null : suggestion}
              ghostHint={t("tab_complete_hint")}
              onGhostKeyDown={(event, atEnd) => {
                if (!suggestion || mutationIsBlocked()) return false;
                if (event.key === "Tab" || event.key === "ArrowRight" && atEnd) { acceptSuggestion(); return true; }
                if (event.key === "Escape") { dismiss(); return true; }
                if (candidates.length > 1 && (event.key === "ArrowUp" || event.key === "ArrowDown")) { cycle(event.key === "ArrowDown" ? 1 : -1); return true; }
                return false;
              }}
            />
          ) : <textarea
            ref={textareaRef}
            autoFocus={autoFocus}
            value={val}
            rows={1}
            disabled={Boolean(disabled && !inSteerMode)}
            readOnly={textareaMutationLocked}
            aria-readonly={textareaMutationLocked}
            aria-disabled={mutationLocked || undefined}
            aria-label={placeholder ?? t("placeholder")}
            onChange={(e) => {
              chartPrefillRef.current = null;
              const nextValue = e.target.value;
              currentValueRef.current = nextValue;
              const latest = latestOrdinaryDraftRef.current;
              if (latest) {
                latestOrdinaryDraftRef.current = {
                  ...latest,
                  draft: { ...latest.draft, message: nextValue },
                };
              }
              setVal(nextValue);
            }}
            onCompositionStart={() => {
              compositionEpochRef.current += 1;
              compositionPhaseRef.current = "composing";
              if (compositionSettleFrameRef.current !== null) {
                window.cancelAnimationFrame(compositionSettleFrameRef.current);
                compositionSettleFrameRef.current = null;
              }
              compositionFencedRef.current = true;
              setCompositionFenced(true);
            }}
            onCompositionEnd={(event) => {
              const epoch = compositionEpochRef.current;
              compositionPhaseRef.current = "awaiting_final_input";
              // Some engines emit the final non-composing input in a later
              // task. Fence every action until either that input arrives or
              // both the native value and input version remain unchanged for
              // two consecutive render frames. This is value/version based,
              // not a fixed timing guess.
              let lastValue = event.currentTarget.value;
              let lastVersion = nativeInputVersionRef.current;
              let stableFrames = 0;
              const observeStableNativeValue = () => {
                if (compositionEpochRef.current !== epoch) return;
                const currentValue = textareaRef.current?.value ?? lastValue;
                const currentVersion = nativeInputVersionRef.current;
                if (currentValue === lastValue && currentVersion === lastVersion) {
                  stableFrames += 1;
                } else {
                  lastValue = currentValue;
                  lastVersion = currentVersion;
                  stableFrames = 0;
                }
                if (stableFrames < 2) {
                  compositionSettleFrameRef.current = window.requestAnimationFrame(
                    observeStableNativeValue,
                  );
                  return;
                }
                setVal(currentValue);
                compositionPhaseRef.current = "idle";
                compositionFencedRef.current = false;
                setCompositionFenced(false);
                compositionSettleFrameRef.current = null;
              };
              compositionSettleFrameRef.current = window.requestAnimationFrame(
                observeStableNativeValue,
              );
            }}
            onInput={(event) => {
              nativeInputVersionRef.current += 1;
              if (
                compositionPhaseRef.current !== "awaiting_final_input"
                || event.nativeEvent.isComposing
              ) return;
              compositionPhaseRef.current = "settling";
              if (compositionSettleFrameRef.current !== null) {
                window.cancelAnimationFrame(compositionSettleFrameRef.current);
                compositionSettleFrameRef.current = null;
              }
              const epoch = compositionEpochRef.current;
              requestAnimationFrame(() => {
                if (compositionEpochRef.current !== epoch) return;
                setVal(textareaRef.current?.value ?? "");
                compositionPhaseRef.current = "idle";
                compositionFencedRef.current = false;
                setCompositionFenced(false);
              });
            }}
            onKeyDown={(e) => {
              // Suppress all shortcuts during IME composition (Pinyin/Japanese/Korean candidate popup open).
              if (e.nativeEvent.isComposing || e.keyCode === 229) return;

              // #88 Autocomplete: Tab accepts suggestion without submitting, Esc dismisses,
              // ↑/↓ cycles candidates (only with >1 candidate); Right-arrow-at-end also accepts.
              if (e.key === "Tab" && suggestion && !mutationIsBlocked()) {
                e.preventDefault();
                acceptSuggestion();
                return;
              }
              if (e.key === "Escape" && suggestion) {
                e.preventDefault();
                dismiss();
                return;
              }
              if (suggestion && candidates.length > 1
                  && !mutationIsBlocked()
                  && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
                e.preventDefault();
                cycle(e.key === "ArrowDown" ? 1 : -1);
                return;
              }
              if (e.key === "ArrowRight" && suggestion
                  && !mutationIsBlocked()
                  && textareaRef.current
                  && textareaRef.current.selectionStart === val.length
                  && textareaRef.current.selectionEnd === val.length) {
                e.preventDefault();
                acceptSuggestion();
                return;
              }

              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder={
              readOnly
                // licensing §13.2 — read-only (over_grace / suspended): sending disabled.
                ? t("readonly_placeholder")
                : inSteerMode
                // prod-bug-bash-038 #11: stronger affordance — user didn't know
                // mid-turn Enter would steer. Make it discoverable + actionable.
                // prod-bug-bash-039 P2-4: thread through t() with locale fallback.
                ? t("steer_placeholder")
                : (placeholder ?? t("placeholder"))
            }
          />}
          {/* Ghost text overlay shares the textarea's font and inset tokens so
              suggestions stay aligned when text size changes on touch devices. */}
          {suggestion && !controlsMutationLocked && !showReferenceEditor ? (
            <div
              aria-hidden
              className="composer-v2__autocomplete-overlay"
            >
              {val}
              {/* Former inline styles moved to styles/ui-controls-legacy.css .legacy-composer-ghost* (workbench/canvas batch 1). */}
              <span data-testid="composer-autocomplete-ghost" className="legacy-composer-ghost">
                {suggestion}
              </span>
              {candidates.length > 1 ? (
                <span className="legacy-composer-ghost-hint legacy-composer-ghost-hint--cycle">
                  [{index + 1}/{candidates.length}] {t("cycle_hint")}
                </span>
              ) : (
                <span className="legacy-composer-ghost-hint legacy-composer-ghost-hint--tab">
                  {t("tab_complete_hint")}
                </span>
              )}
            </div>
          ) : null}
        </div>

        <input
          ref={fileRef}
          type="file"
          multiple
          accept={UPLOAD_ACCEPT}
          style={{ display: "none" }}
          disabled={uploadDisabled}
          onChange={onFileChange}
        />

        <div className="composer-v2__toolbar">
          <div className="composer-v2__tools">
            {readOnly ? (
              <EntitlementReadOnlyChip />
            ) : layoutMode === "comfortable" ? (
              <>
                {googleDriveAvailable ? (
                  <ComposerAddMenu
                    disabled={controlsMutationLocked}
                    uploadDisabled={uploadDisabled}
                    fallbackFocusRef={textareaRef}
                    onUpload={pickFiles}
                    onPickGoogleDrive={pickFromGoogleDrive}
                  />
                ) : (
                  <button
                    type="button"
                    className="composer-v2__tool-button"
                    title={t("upload_title")}
                    onClick={pickFiles}
                    disabled={uploadDisabled}
                    aria-label={t("attach")}
                  >
                    <ComposerGlyph kind="add" />
                  </button>
                )}
                <ModelPicker
                  disabled={Boolean((disabled && !inSteerMode) || controlsMutationLocked)}
                  fallbackFocusRef={textareaRef}
                  telemetry={{ surface, composerMode: layoutMode }}
                />
                {composerCapabilities.planning ? (
                  <PlanningControl
                    disabled={controlsMutationLocked}
                    onToggle={(enabled) => {
                      trackComposerEvent({
                        event_name: "composer_planning_toggled",
                        enabled,
                        entry: "toolbar",
                        surface,
                        composer_mode: layoutMode,
                      });
                    }}
                  />
                ) : null}
              </>
            ) : (
              <>
                <button
                  ref={toolsTriggerRef}
                  type="button"
                  className={`composer-v2__tool-button${planningMode === "planning" ? " is-active" : ""}`}
                  onClick={() => {
                    if (!toolsOpen) {
                      trackComposerEvent({
                        event_name: "composer_tools_opened",
                        surface,
                        composer_mode: layoutMode === "ultra" ? "ultra" : "compact",
                      });
                    }
                    setToolsOpen(true);
                  }}
                  disabled={controlsMutationLocked}
                  aria-label={
                    planningMode === "planning" && composerCapabilities.planning
                      ? t("tools_planning_active")
                      : t("tools_title")
                  }
                  aria-haspopup="dialog"
                  aria-expanded={toolsOpen}
                  aria-controls={toolsDialogId}
                >
                  <ComposerGlyph kind="add" />
                </button>
                <ModelPicker
                  disabled={Boolean((disabled && !inSteerMode) || controlsMutationLocked)}
                  fallbackFocusRef={textareaRef}
                  telemetry={{ surface, composerMode: layoutMode }}
                />
                {/* ★2026-08-14: remove the `planningMode === "planning"` condition.
                    With narrow columns + auto/off, the control **entirely vanished from the toolbar**, retreating into the ＋ panel,
                    prompting bug reports about the missing narrow-column planning entry. DEFAULT_SPLIT_RATIO
                    remains 0.4, so opening canvas puts the left column in compact; this is no edge case.
                    ★Also caught on device 116 that day: this originally checked only `=== "compact"`, omitting ultra (<360).
                    The planning row in the ＋ panel was removed in that same change, leaving ultra
                    **no planning-mode entry anywhere on the page**, worse than before (which still had the panel).
                    CSS already specifies 44×44 in both compact/ultra; only this render guard was missing. */}
                {(layoutMode === "compact" || layoutMode === "ultra") &&
                composerCapabilities.planning ? (
                    <PlanningControl
                      compact
                      disabled={controlsMutationLocked}
                      onToggle={(enabled) => {
                        trackComposerEvent({
                          event_name: "composer_planning_toggled",
                          enabled,
                          entry: "toolbar",
                          surface,
                          composer_mode: layoutMode,
                        });
                      }}
                    />
                  ) : null}
              </>
            )}
          </div>
          <div className="composer-v2__actions">
            {readOnly ? null : primaryAction}
            {readOnly ? null : (
              <span id={actionTooltipId} className="composer-v2__tooltip" role="tooltip">
                {primaryActionTooltip}
              </span>
            )}
          </div>
        </div>
        <ComposerToolsSheet
          open={
            toolsOpen
            && layoutMode !== "comfortable"
            && !readOnly
            && !controlsMutationLocked
          }
          dialogId={toolsDialogId}
          triggerRef={toolsTriggerRef}
          planning={composerCapabilities.planning}
          onUploadChange={onFileChange}
          uploadDisabled={uploadDisabled}
          onPickGoogleDrive={googleDriveAvailable ? pickFromGoogleDrive : undefined}
          onPlanningToggle={(enabled) => {
            trackComposerEvent({
              event_name: "composer_planning_toggled",
              enabled,
              entry: "tools_sheet",
              surface,
              composer_mode: layoutMode,
            });
          }}
          onClose={() => setToolsOpen(false)}
        />
      </div>
      <ComposerNotices notices={composerNotices} />
    </div>
  );
}

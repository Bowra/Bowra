"use client";

import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";
import { DocumentDraftReview } from "@/components/documents/document-draft-review";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Notice } from "@/components/ui/notice";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { CanvasSection, SectionHeading } from "@/components/ui/section-heading";
import type { RowAction } from "@/components/ui/row-actions-menu";
import { Chat, AlertCircle, CircleX, FileText, Info, Lock, RefreshCw } from "@/components/icons";
import { MessageSquare } from "lucide-react";
import { ChatCard } from "@/components/ui/card";
import {
  CanvasObjectFooter,
  CanvasObjectHeader,
  CanvasObjectSurface,
  CanvasObjectToolbar,
  ObjectStatusBadge,
  StateBlock,
  ToolbarAction,
  type CanvasObjectStatus,
} from "@/components/canvas/object";
import styles from "./document-draft-canvas-object.module.css";
import { documentErrorKey, documentFailureKey } from "@/components/chat/document-errors";
import { ApiError } from "@/lib/api-client";
import {
  completeDocumentCanvasContext,
  confirmDocumentDraft,
  documentCanvasIdentity,
  documentConfirmationId,
  readDocumentDraft,
  type DocumentDraftCanvasContext,
} from "@/lib/document-draft-client";
import { documentCanvasTitle, documentCanvasStateKey, documentSourceExtension, type DocumentJob, type DocumentPreviewAccess } from "@/lib/document-drafts";
import { randomUuid } from "@/lib/random-id";
import type { DocumentRevisionDisplay, DocumentRevisionTarget, DocumentRevisionStageResult } from "@/lib/composer-document-revision";
import { spaceHeader } from "@/lib/space-storage";

export type { DocumentDraftCanvasContext } from "@/lib/document-draft-client";

export type DocumentDraftCanvasPhase =
  | "loading"
  | "ready"
  | "error"
  | "unavailable"
  | "confirming"
  | "confirmation_unknown";
export interface DocumentDraftCanvasUpdate {
  context: Readonly<DocumentDraftCanvasContext>;
  phase: DocumentDraftCanvasPhase;
  title: string;
  status: string;
  /** C11 status (drives the toolbar badge and the C1 tab marker). */
  objectStatus: CanvasObjectStatus | null;
  state: DocumentJob["state"] | null;
  closePolicy: "detach";
}
export interface DocumentDraftCanvasPublication {
  context: Readonly<DocumentDraftCanvasContext>;
  originTurnId: string;
  draftDigest: string;
  source: { artifactId: string; sha256: string };
  pdf: { artifactId: string; sha256: string };
}
export interface DocumentDraftCanvasRevisionRequest {
  context: Readonly<DocumentDraftCanvasContext>;
  target: DocumentRevisionTarget;
  handoffId: string;
  /** Draft name and number for the Composer revision bar (display only). */
  display?: DocumentRevisionDisplay;
  /** Only the original mounted reader can authorize this handoff. */
  isCurrent: () => boolean;
  mayReveal: () => boolean;
}
export interface DocumentDraftCanvasObjectProps {
  context: Readonly<DocumentDraftCanvasContext>;
  /** Registered instance in the current auth scope, independent of Tab selection. */
  isCurrent: (context: Readonly<DocumentDraftCanvasContext>) => boolean;
  refreshKey?: number;
  onStateChange?: (update: DocumentDraftCanvasUpdate) => void;
  onPublished?: (result: DocumentDraftCanvasPublication) => void;
  onOpenPublished?: (context: Readonly<DocumentDraftCanvasContext>, artifactId: string) => void;
  onRequestRevision?: (request: DocumentDraftCanvasRevisionRequest) => Promise<DocumentRevisionStageResult>;
  onReturnToChat?: (context: Readonly<DocumentDraftCanvasContext>) => void;
  /** Puts the original request back into the composer; the user sends it explicitly. */
  onRegenerateInChat?: (context: Readonly<DocumentDraftCanvasContext>, requestText: string) => void;
}

type View = {
  phase: DocumentDraftCanvasPhase;
  job: DocumentJob | null;
  error?: string;
};
type Confirmation = { digest: string; commandId: string; dispatched: boolean };
const isDenied = (error: unknown) =>
  error instanceof ApiError && [401, 403, 404].includes(error.status);
const isActive = (state: DocumentJob["state"]) =>
  ["queued", "preparing", "planning", "rendering"].includes(state);

export function DocumentDraftCanvasObject(
  props: DocumentDraftCanvasObjectProps,
) {
  const t = useTranslations("documents");
  if (
    !completeDocumentCanvasContext(props.context) ||
    !props.isCurrent(props.context)
  ) {
    return (
      <CanvasObjectSurface label={t("title")}>
        <StateBlock icon={<Lock size={20} />} title={t("canvas_unavailable_title")} body={t("error_access")} />
      </CanvasObjectSurface>
    );
  }
  return (
    <DocumentDraftCanvasInstance
      key={documentCanvasIdentity(props.context)}
      {...props}
    />
  );
}

function DocumentDraftCanvasInstance(props: DocumentDraftCanvasObjectProps) {
  const t = useTranslations("documents");
  const locale = useLocale();
  const common = useTranslations("canvas_object");
  const [context] = useState(() => Object.freeze({ ...props.context }));
  const callbacks = useRef(props);
  callbacks.current = props;
  const [view, setView] = useState<View>({ phase: "loading", job: null });
  const current = useRef(view);
  const mounted = useRef(false);
  const revoked = useRef(false);
  const operation = useRef<AbortController | null>(null);
  const commandBusy = useRef(false);
  const attempt = useRef<Confirmation | null>(null);
  const publishedDigest = useRef<string | null>(null);
  const revisionHandoff = useRef<{ digest: string; id: string } | null>(null);
  const [staging, setStaging] = useState(false);
  const [revisionNotice, setRevisionNotice] = useState<string | null>(null);

  function inScope() {
    return mounted.current && callbacks.current.isCurrent(context);
  }
  function accepts(controller: AbortController) {
    return (
      inScope() &&
      operation.current === controller &&
      !controller.signal.aborted &&
      !revoked.current
    );
  }
  function update(next: View) {
    current.current = next;
    setView(next);
  }
  function denyAccess() {
    if (!inScope()) return;
    revoked.current = true;
    operation.current?.abort();
    commandBusy.current = false;
    update({ phase: "unavailable", job: null });
  }
  function install(job: DocumentJob, error?: string) {
    const pending = attempt.current;
    if (
      pending?.dispatched &&
      job.state === "published" &&
      job.draft_digest !== pending.digest
    ) {
      update({
        phase: "confirmation_unknown",
        job: null,
        error: "error_stale",
      });
      return;
    }
    const unknown =
      pending?.dispatched &&
      job.state === "review" &&
      job.draft_digest === pending.digest;
    if (!unknown) attempt.current = null;
    update({ phase: unknown ? "confirmation_unknown" : "ready", job, error });
  }
  async function read(controller: AbortController, error?: string) {
    try {
      const job = await readDocumentDraft(context, controller.signal);
      if (accepts(controller)) install(job, error);
    } catch (cause) {
      if (!accepts(controller)) return;
      if (isDenied(cause)) {
        denyAccess();
        return;
      }
      update({
        phase: attempt.current?.dispatched ? "confirmation_unknown" : "error",
        job: current.current.job,
        error: "canvas_read_error",
      });
    }
  }
  function reload() {
    if (!inScope()) return;
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    revoked.current = false;
    commandBusy.current = false;
    setStaging(false);
    setRevisionNotice(null);
    // Keep terminal context and its focused controls through a read-only status check.
    update({ phase: "loading", job: current.current.job?.state === "failed" ? current.current.job : null });
    void read(controller);
  }

  function checkStatus() {
    if (!inScope() || commandBusy.current || staging || current.current.phase === "loading" || current.current.phase === "confirming") return;
    reload();
  }

  useEffect(() => {
    mounted.current = true;
    reload();
    return () => {
      mounted.current = false;
      operation.current?.abort();
    };
    // The host owns invalidation; callbacks and request state are read from refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.refreshKey]);

  const previewAccess = useMemo<DocumentPreviewAccess>(
    () => ({
      headers: spaceHeader(context),
      isCurrent: () => inScope() && !revoked.current,
      onAccessDenied: denyAccess,
      // The keyed instance captures one immutable context; live callbacks use refs.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }),
    [context],
  );

  const status =
    view.phase === "unavailable"
      ? t("error_access")
      : view.phase === "confirming"
        ? t("canvas_confirming")
        : view.phase === "confirmation_unknown"
          ? t("canvas_confirmation_unknown")
          : view.phase === "loading"
            ? t("canvas_loading")
            : view.phase === "error"
              ? t(view.error ?? "canvas_read_error")
              : view.job
                ? t(documentCanvasStateKey(view.job))
                : t("canvas_loading");
  useEffect(() => {
    if (!inScope()) return;
    callbacks.current.onStateChange?.({
      context,
      phase: view.phase,
      title: view.job ? documentCanvasTitle(view.job, t("title")) : t("title"),
      status,
      objectStatus: documentObjectStatus(view.phase, view.job),
      state: view.job?.state ?? null,
      closePolicy: "detach",
    });
    const job = view.job;
    if (
      job?.state !== "published" ||
      !job.draft_digest ||
      !job.draft ||
      publishedDigest.current === job.draft_digest
    )
      return;
    const source = job.draft.files["candidate.pptx"];
    const pdf = job.draft.files["candidate.pdf"];
    if (!source || !pdf || !inScope() || !callbacks.current.onPublished) return;
    publishedDigest.current = job.draft_digest;
    callbacks.current.onPublished?.({
      context,
      originTurnId: job.origin_turn_id,
      draftDigest: job.draft_digest,
      source: { artifactId: source.artifact_id, sha256: source.sha256 },
      pdf: { artifactId: pdf.artifact_id, sha256: pdf.sha256 },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, status, context, t]);

  async function confirm(draftDigest: string, retryOriginal = false) {
    const original = current.current.job;
    if (
      !inScope() ||
      revoked.current ||
      commandBusy.current ||
      current.current.phase === "loading" ||
      (current.current.phase === "confirmation_unknown" && !retryOriginal) ||
      !original?.can_confirm ||
      original.state !== "review" ||
      original.draft_digest !== draftDigest
    )
      return;
    commandBusy.current = true;
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    update({ phase: "confirming", job: original });
    let sent = false;
    try {
      const prior = attempt.current;
      const commandId =
        prior?.digest === draftDigest
          ? prior.commandId
          : await documentConfirmationId(context, draftDigest);
      if (
        !accepts(controller) ||
        current.current.job?.draft_digest !== draftDigest
      )
        return;
      attempt.current = {
        digest: draftDigest,
        commandId,
        dispatched: prior?.digest === draftDigest && prior.dispatched,
      };
      const fresh = await readDocumentDraft(context, controller.signal);
      if (!accepts(controller)) return;
      if (
        fresh.state !== "review" ||
        fresh.draft_digest !== draftDigest ||
        !fresh.can_confirm
      ) {
        install(
          fresh,
          fresh.state === "published" && fresh.draft_digest === draftDigest
            ? undefined
            : "error_stale",
        );
        return;
      }
      if (!accepts(controller)) return;
      attempt.current.dispatched = true;
      sent = true;
      const published = await confirmDocumentDraft(
        context,
        draftDigest,
        commandId,
        controller.signal,
      );
      if (accepts(controller)) {
        attempt.current = null;
        install(published);
      }
    } catch (cause) {
      if (!accepts(controller)) return;
      if (isDenied(cause)) {
        denyAccess();
        return;
      }
      const rejected =
        sent &&
        cause instanceof ApiError &&
        !cause.nonApiResponse &&
        [400, 409, 422].includes(cause.status);
      if (rejected) attempt.current = null;
      if (!sent && !attempt.current?.dispatched) {
        update({ phase: "error", job: original, error: "canvas_read_error" });
      } else {
        update({
          phase: rejected ? "error" : "confirmation_unknown",
          job: original,
          error: rejected ? documentErrorKey(cause.code) : undefined,
        });
        await read(
          controller,
          rejected ? documentErrorKey(cause.code) : undefined,
        );
      }
    } finally {
      if (operation.current === controller) commandBusy.current = false;
    }
  }

  async function requestRevision() {
    const original = current.current.job;
    const callback = callbacks.current.onRequestRevision;
    if (
      !inScope() ||
      revoked.current ||
      commandBusy.current ||
      current.current.phase !== "ready" ||
      !original?.can_revise ||
      original.state !== "review" ||
      !original.draft_digest ||
      !callback
    )
      return;
    const digest = original.draft_digest;
    const originFocus = document.activeElement;
    commandBusy.current = true;
    setStaging(true);
    setRevisionNotice(null);
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    try {
      const fresh = await readDocumentDraft(context, controller.signal);
      if (!accepts(controller)) return;
      if (
        !fresh.can_revise ||
        fresh.state !== "review" ||
        fresh.draft_digest !== digest
      ) {
        install(fresh, "error_stale");
        return;
      }
      install(fresh);
      if (revisionHandoff.current?.digest !== digest)
        revisionHandoff.current = { digest, id: randomUuid() };
      const displayTitle = documentCanvasTitle(fresh, "");
      const result = await callback({
        context,
        handoffId: revisionHandoff.current.id,
        ...(displayTitle
          ? { display: { title: displayTitle, ...(fresh.draft_version ? { version: fresh.draft_version } : {}) } }
          : {}),
        target: {
          kind: "document_revision",
          version: 1,
          session_id: context.sessionId,
          job_id: context.jobId,
          draft_digest: digest,
        },
        isCurrent: () =>
          accepts(controller) &&
          current.current.job?.draft_digest === digest &&
          current.current.job.can_revise === true,
        mayReveal: () =>
          document.activeElement === originFocus ||
          document.activeElement === document.body,
      });
      if (accepts(controller))
        setRevisionNotice(
          result.status === "applied"
            ? "canvas_revision_staged"
            : result.status === "blocked"
              ? `canvas_revision_${result.reason}`
              : "error_stale",
        );
    } catch (cause) {
      if (!accepts(controller)) return;
      if (isDenied(cause)) denyAccess();
      else setRevisionNotice("canvas_read_error");
    } finally {
      if (operation.current === controller) {
        commandBusy.current = false;
        if (inScope()) setStaging(false);
      }
    }
  }

  const job = view.job;
  const busy =
    staging ||
    ["loading", "confirming", "confirmation_unknown"].includes(view.phase);
  const titleId = useId();
  const [info, setInfo] = useState<"preview" | "usage" | null>(null);
  const objectStatus = documentObjectStatus(view.phase, job);
  const title = job ? documentCanvasTitle(job, t("title")) : t("title");
  const pages = job?.draft?.preview.pages.length ?? 0;
  const changeCount = job?.draft?.manifest.changes.length ?? 0;
  const stepIndex = job ? ACTIVE_STEPS.indexOf(job.state) : -1;
  const returnToChat = props.onReturnToChat ? () => {
    if (inScope() && !revoked.current) callbacks.current.onReturnToChat?.(context);
  } : undefined;
  const regenerate = props.onRegenerateInChat && job ? () => {
    if (inScope() && !revoked.current && current.current.job) callbacks.current.onRegenerateInChat?.(context, current.current.job.request_text);
  } : undefined;
  const usageNote = job?.state === "failed" && !job.usage ? t("canvas_usage_unavailable")
    : job?.usage?.settlement_pending ? t("settlement_pending")
      : job?.usage?.pending ? t("usage_pending") : null;

  const overflow: RowAction[] = [
    { id: "refresh", label: common("refresh"), icon: <RefreshCw size={14} aria-hidden />, disabled: view.phase === "loading" || staging || view.phase === "confirming", onSelect: checkStatus },
    ...(job?.draft ? [{ id: "preview", label: t("preview_details_menu"), icon: <Info size={14} aria-hidden />, onSelect: () => setInfo("preview") }] : []),
    ...(job?.usage ? [{ id: "usage", label: t("usage_menu"), icon: <Info size={14} aria-hidden />, onSelect: () => setInfo("usage") }] : []),
  ];
  const revise = job?.state === "review" && job.can_revise && props.onRequestRevision ? (
    <ToolbarAction
      icon={<Chat size={14} aria-hidden />}
      label={t(staging ? "canvas_revision_preparing" : "canvas_revision_action")}
      disabled={busy || view.phase !== "ready"}
      onClick={() => void requestRevision()}
    />
  ) : null;

  let body: ReactNode;
  if (view.phase === "unavailable") {
    body = <StateBlock icon={<Lock size={20} />} title={t("canvas_unavailable_title")} body={t("error_access")}
      secondaryActions={[{ label: common("refresh"), onClick: checkStatus }]} />;
  } else if (!job) {
    body = view.phase === "error"
      ? <StateBlock tone="error" icon={<AlertCircle size={20} />} title={t("canvas_read_error_title")} body={t(view.error ?? "canvas_read_error")}
        secondaryActions={[{ label: t("canvas_check_status"), onClick: checkStatus }]} />
      : view.phase === "confirmation_unknown"
        ? <StateBlock icon={<Info size={20} />} title={t("canvas_confirmation_unknown_title")} body={t("canvas_confirmation_unknown")} />
        : <LoadingSkeleton variant="form" rows={4} label={t("canvas_loading")} />;
  } else if (view.phase === "confirmation_unknown") {
    body = <StateBlock icon={<Info size={20} />} title={t("canvas_confirmation_unknown_title")} body={t("canvas_confirmation_unknown")} />;
  } else if (isActive(job.state)) {
    body = <StateBlock tone="progress" title={t("working_title")} body={t("working")} />;
  } else if (job.state === "failed") {
    body = (
      <StateBlock
        tone="error"
        icon={<AlertCircle size={20} />}
        title={t("failed_title")}
        body={t(documentFailureKey(job.error_code))}
        secondaryActions={[
          { label: t("canvas_check_status"), onClick: checkStatus, busy: view.phase === "loading", disabled: busy },
          ...(regenerate ? [{ label: t("regenerate_in_chat"), onClick: regenerate }] : []),
        ]}
        primaryAction={returnToChat ? { label: t("canvas_return_to_chat"), onClick: returnToChat } : undefined}
        note={usageNote}
      />
    );
  } else if (job.state === "cancelled") {
    body = <StateBlock icon={<CircleX size={20} />} title={t("cancelled_title")} body={t("cancelled_body")}
      secondaryActions={returnToChat ? [{ label: t("canvas_return_to_chat"), onClick: returnToChat }] : undefined} />;
  } else if (job.state === "question") {
    body = (
      <ChatCard
        intent="input"
        eyebrow={t("question_eyebrow")}
        title={job.question}
        data-testid="document-question-card"
        secondary={returnToChat ? [{ label: t("answer_in_chat"), icon: MessageSquare, variant: "secondary", onSelect: returnToChat }] : undefined}
      >
        <p className="ui-card__text">{t("answer_help")}</p>
      </ChatCard>
    );
  } else {
    body = null;
  }

  const published = job?.state === "published";
  // C2: a review draft shows when it was generated (“10月5日 14:20 生成”).
  const generatedAt = job?.state === "review" && job.updated_at ? formatGeneratedTime(job.updated_at, locale) : null;
  return (
    <CanvasObjectSurface
      labelledBy={titleId}
      width="document"
      busy={staging || view.phase === "loading" || view.phase === "confirming"}
      toolbar={(
        <CanvasObjectToolbar
          status={objectStatus ? <ObjectStatusBadge status={objectStatus} /> : null}
          statusInBodyWhenNarrow={job?.state === "review"}
          meta={job && stepIndex >= 0 ? t("working_step", { state: t(documentCanvasStateKey(job)), n: stepIndex + 1 })
            : generatedAt ? t("canvas_generated_at", { time: generatedAt }) : undefined}
          actions={revise}
          overflow={overflow}
        />
      )}
      footer={view.phase === "confirmation_unknown" && job?.can_confirm && attempt.current ? (
        <CanvasObjectFooter
          secondary={[{ label: t("canvas_check_status"), onClick: checkStatus }]}
          primary={{ label: t("canvas_retry_confirmation"), onClick: () => { if (attempt.current) void confirm(attempt.current.digest, true); } }}
        />
      ) : undefined}
    >
      <CanvasObjectHeader
        titleId={titleId}
        icon={<FileText size={14} aria-hidden />}
        eyebrow={t("canvas_eyebrow", { format: (job ? documentSourceExtension(job) : null)?.toUpperCase() ?? "PPTX" })}
        title={title}
        subtitle={job?.draft ? t("canvas_subtitle", { pages, changes: changeCount }) : undefined}
      />
      {view.error && view.phase !== "error" ? <Notice tone="error">{t(view.error)}</Notice> : null}
      {view.phase === "error" && job ? <Notice tone="error">{t(view.error ?? "canvas_read_error")}</Notice> : null}
      {revisionNotice && view.phase !== "unavailable" ? <Notice tone="muted">{t(revisionNotice)}</Notice> : null}
      {body}
      {/* The object header right above already names the file, so the result card carries no second title. */}
      {published ? (
        <ChatCard
          intent="success"
          eyebrow={t("state_published")}
          data-testid="document-published-card"
          secondary={props.onOpenPublished ? (["pptx", "pdf"] as const).flatMap((format) => {
            const artifactId = job.draft?.files[`candidate.${format}`]?.artifact_id;
            return artifactId ? [{
              label: t(format === "pptx" ? "canvas_open_pptx" : "canvas_open_pdf"),
              variant: "secondary" as const,
              disabled: busy || view.phase !== "ready",
              onSelect: () => {
                if (inScope() && !revoked.current && current.current.job === job) callbacks.current.onOpenPublished?.(context, artifactId);
              },
            }] : [];
          }) : undefined}
        >
          <dl className="ui-card-facts">
            <dt>{t("published_content")}</dt>
            <dd>{t("canvas_subtitle", { pages, changes: changeCount })}</dd>
            {job.usage ? <>
              <dt>{t("published_usage")}</dt>
              <dd>{t("credits_value", { credits: job.usage.settled_credits })}</dd>
            </> : null}
          </dl>
          {usageNote ? <p className="ui-card__note">{usageNote}</p> : null}
        </ChatCard>
      ) : null}
      {job && (job.state === "review" || job.state === "published") && view.phase !== "unavailable" ? (
        <DocumentDraftReview
          job={job}
          busy={busy}
          publishing={view.phase === "confirming"}
          previewAccess={previewAccess}
          status={objectStatus && job.state === "review" ? <ObjectStatusBadge status={objectStatus} /> : undefined}
          onConfirm={(value) => void confirm(value)}
        />
      ) : null}
      {job && view.phase !== "unavailable" ? <RequestSection text={job.request_text} /> : null}
      {info ? (
        <Dialog
          title={info === "preview" ? t("preview_details") : t("usage_title")}
          onClose={() => setInfo(null)}
          footer={<Button onClick={() => setInfo(null)}>{t("info_close")}</Button>}
        >
          <p>{info === "preview" ? t("renderer_note") : job?.usage ? t("canvas_settled_credits", { credits: job.usage.settled_credits }) : null}</p>
          {info === "usage" && usageNote ? <p>{usageNote}</p> : null}
        </Dialog>
      ) : null}
    </CanvasObjectSurface>
  );
}

const ACTIVE_STEPS: DocumentJob["state"][] = ["queued", "preparing", "planning", "rendering"];

/** C11 status for the toolbar badge; the host derives the tab marker from the same value. */
export function documentObjectStatus(phase: DocumentDraftCanvasPhase, job: DocumentJob | null): CanvasObjectStatus | null {
  if (phase === "unavailable") return null;
  if (phase === "confirming") return "publishing";
  if (phase === "confirmation_unknown") return "unconfirmed";
  if (!job) return phase === "loading" ? null : null;
  if (isActive(job.state)) return "generating";
  if (job.state === "review") return job.can_confirm ? "review" : "read_only";
  if (job.state === "question") return "needs_input";
  if (job.state === "published") return "published";
  if (job.state === "failed") return "failed";
  if (job.state === "cancelled") return "cancelled";
  return null;
}

function RequestSection({ text }: { text: string }) {
  const t = useTranslations("documents");
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <CanvasSection labelledBy={`${id}-title`}>
      <SectionHeading id={`${id}-title`} title={t("source_request")} collapsible open={open} controls={`${id}-body`} onToggle={() => setOpen((value) => !value)} />
      {open ? <p id={`${id}-body`} className={styles.request}>{text}</p> : null}
    </CanvasSection>
  );
}

function formatGeneratedTime(value: string, locale: string): string | null {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

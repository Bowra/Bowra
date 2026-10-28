"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  composerDraftScopeSignature,
  type ComposerPersistenceScope,
} from "@/lib/composer-persistence-scope";
import {
  readComposerReferenceHandoff,
  type ComposerReferenceSnapshot,
} from "@/lib/composer-reference-transfer";
import { readComposerInputTransientDraft } from "@/lib/composer-transient-draft";
import { composerPersistenceNeedsUpgrade } from "@/lib/composer-local-persistence-db";
import { acceptComposerReferenceHandoff, discardComposerReferenceHandoff } from "@/lib/composer-reference-handoff";
import { ComposerReferencePreview } from "./composer-reference-preview";
import type { ComposerNoticeItem } from "./composer-notices";
import { retryComposerDraftHandoff, waitForComposerDraftHandoff } from "@/lib/composer-draft-handoff";

/** Shared by home and Workbench, before any text consumer unlocks. A transfer
 * is only previewed: it cannot overwrite an ordinary draft or settle an action.
 */
export function ComposerReferenceTransferBoundary({
  scope,
  children,
  consumeReferenceTransient = false,
  allowHandoffAcceptance = false,
}: {
  scope: ComposerPersistenceScope | null;
  /** `notice` is the transfer warning: the composer shows it in its single notice slot (spec §9, D9). */
  children: (locked: boolean, notice: ComposerNoticeItem | null) => ReactNode;
  consumeReferenceTransient?: boolean;
  allowHandoffAcceptance?: boolean;
}) {
  const t = useTranslations("composer");
  const signature = composerDraftScopeSignature(scope);
  const [retry, setRetry] = useState(0);
  const [pending, setPending] = useState(false);
  const [handoffConflict, setHandoffConflict] = useState(false);
  const [state, setState] = useState<{
    signature: string;
    snapshots: { source: "transient" | "handoff"; draft: ComposerReferenceSnapshot }[];
    failed: boolean;
    versionBlocked: boolean;
    handoffFailed?: boolean;
  } | null>(null);
  useEffect(() => {
    if (!scope) return;
    let current = true;
    // Preserve each independently: an unreadable handoff must not hide the
    // recoverable transient, or vice versa.
    void waitForComposerDraftHandoff(signature).then(() => Promise.allSettled([
      Promise.resolve().then(() => {
        const draft = readComposerInputTransientDraft(signature);
        // The input owner restores exact revisions; the reference preview must not strip their target.
        return draft && "schemaVersion" in draft && draft.schemaVersion === 2 ? draft : null;
      }),
      readComposerReferenceHandoff(scope),
    ])).then((results) => {
      if (!current) return;
      const snapshots: NonNullable<typeof state>["snapshots"] = [];
      for (const [index, result] of results.entries()) {
        if (result.status === "fulfilled" && result.value && !(index === 0 && consumeReferenceTransient)) {
          snapshots.push({ source: index === 0 ? "transient" : "handoff", draft: result.value });
        }
      }
      setState({
        signature,
        snapshots,
        failed: results.some((result) => result.status === "rejected"),
        versionBlocked: results.some(
          (result) =>
            result.status === "rejected" && composerPersistenceNeedsUpgrade(result.reason),
        ),
      });
    }).catch(() => {
      if (current) setState({ signature, snapshots: [], failed: true, versionBlocked: false, handoffFailed: true });
    });
    return () => {
      current = false;
    };
  }, [scope, signature, retry, consumeReferenceTransient]);
  const resolveHandoff = async (draft: ComposerReferenceSnapshot, keepCurrent: boolean) => {
    if (!scope || pending || !allowHandoffAcceptance || state?.signature !== signature || state.failed || state.versionBlocked) return;
    setPending(true);
    try {
      const resolved = keepCurrent
        ? await discardComposerReferenceHandoff(scope, draft)
        : await acceptComposerReferenceHandoff(scope, draft).then((result) => result.status === "missing" || result.status === "saved" && result.acknowledged);
      setHandoffConflict(!resolved);
      if (resolved) {
        setState(null);
        setRetry((value) => value + 1);
      }
    } catch {
      setHandoffConflict(true);
    } finally {
      setPending(false);
    }
  };
  // Keep one tree shape with and without a scope: returning the children on
  // their own when there is no scope would remount the composer as soon as
  // the first scope resolves.
  const ready = scope !== null && state?.signature === signature;
  const locked = scope !== null && (!ready || state.failed || state.snapshots.length > 0);
  const notice: ComposerNoticeItem | null = ready && locked
    ? {
        id: "reference-transfer",
        tone: "error",
        text: t(
          state.handoffFailed
            ? "draft_handoff_blocked"
            : state.versionBlocked
              ? "draft_version_blocked"
              : state.failed
                ? "reference_transfer_read_failed"
                : "reference_saved_locked",
        ),
        actions: [{
          label: t(state.handoffFailed ? "draft_handoff_retry" : "draft_retry"),
          onSelect: () => {
            retryComposerDraftHandoff(signature);
            setState(null);
            setRetry((value) => value + 1);
          },
        }],
      }
    : null;
  return (
    <>
      {ready
        ? state.snapshots.map(({ source, draft }) => (
            <div key={source} data-reference-transfer={source}>
              <ComposerReferencePreview
                title={t(
                  source === "handoff" ? "reference_handoff_title" : "reference_transient_title",
                )}
                document={draft.input_document}
                attachments={draft.attachments}
              />
              {source === "handoff" && allowHandoffAcceptance && !state.failed && !state.versionBlocked ? <div className="composer-v2__draft-warning-actions">
                <button type="button" disabled={pending} onClick={() => void resolveHandoff(draft, false)}>{t("reference_accept_handoff")}</button>
                <button type="button" disabled={pending} onClick={() => void resolveHandoff(draft, true)}>{t("reference_keep_existing")}</button>
                {handoffConflict ? <span role="alert">{t("reference_handoff_conflict")}</span> : null}
              </div> : null}
            </div>
          ))
        : null}
      {children(locked, notice)}
    </>
  );
}

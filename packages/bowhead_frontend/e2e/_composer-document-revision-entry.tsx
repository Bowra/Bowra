import { StrictMode, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { AuthSessionProvider } from "../lib/auth/auth-session-context";
import { V2Composer, type ComposerDraftControl, type ComposerDraftSubmissionContext } from "../components/chat/v2-composer";
import { useTurnRunner, type UseTurnRunnerArgs } from "../lib/use-turn-runner";
import * as drafts from "../lib/composer-ordinary-draft";
import { readPendingSubmission, type ComposerSubmissionWireSnapshot } from "../lib/composer-submission-recovery";
import { composerDraftScopeSignature, type ComposerPersistenceScope } from "../lib/composer-persistence-scope";
import type { DocumentRevisionStageRequest, DocumentRevisionStageResult } from "../lib/composer-document-revision";
import en from "../messages/en-US.json";

const scope: ComposerPersistenceScope = { userId: "u_revision", teamId: "team_revision", workspaceId: "ws_revision", requestedSessionId: "s_review", authSessionGeneration: "original" };
const wire: ComposerSubmissionWireSnapshot = { composerSurface: "workbench", userProfile: null, preferredProvider: null, preferredModel: null, planningMode: "off" };
const target = { kind: "document_revision" as const, version: 1 as const, session_id: "s_review", job_id: "djob_original", draft_digest: "a".repeat(64) };
type Settings = { epoch: number; otherScope: boolean; running: boolean; locked: boolean; cleanupFailure: boolean; otherCanvas: boolean; prefill: string };
const controller = {
  configure: (_patch: Partial<Settings>) => {},
  ready: () => false,
  stage: async (_patch: Partial<DocumentRevisionStageRequest> = {}): Promise<DocumentRevisionStageResult> => ({ status: "stale" }),
  recover: async () => {},
  calls: [] as { message: string; context: ComposerDraftSubmissionContext | null }[],
  settled: [] as boolean[],
  read: async () => ({ draft: await drafts.readComposerInputDraft(scope), pending: await readPendingSubmission(scope) }),
};
declare global { interface Window { revisionComposer: typeof controller } }
window.revisionComposer = controller;
function Harness() {
  const [settings, setSettings] = useState<Settings>({ epoch: 0, otherScope: false, running: false, locked: false, cleanupFailure: false, otherCanvas: false, prefill: "" });
  const [intent, setIntent] = useState<Partial<UseTurnRunnerArgs>>({});
  const control = useRef<ComposerDraftControl | null>(null);
  const resolvers = useRef(new Map<string, (accepted: boolean) => void>());
  const sequence = useRef(0);
  const active = settings.otherScope ? { ...scope, workspaceId: "ws_other", requestedSessionId: "s_other" } : scope;
  controller.configure = (patch) => setSettings((previous) => ({ ...previous, ...patch }));
  controller.ready = () => Boolean(control.current);
  controller.stage = async (patch = {}) => control.current!.stageDocumentRevision({ handoffId: "original", scope, target, instruction: "Shorten the title", ...patch });
  const runner = useTurnRunner({
    ...intent, mode: "sse", workspaceId: active.workspaceId, activeSessionId: active.requestedSessionId,
    submissionProtocolEnabled: false, referenceSubmissionEnabled: false, submissionRecoveryScope: active,
    currentChatScopeKey: composerDraftScopeSignature(active),
    onSubmissionPending: (_attempt, _scope, pending) => {
      // A presentation consumer must never mutate the already captured wire.
      if (pending.wireSnapshot?.documentRevisionTarget) pending.wireSnapshot.documentRevisionTarget.job_id = "djob_observer_mutation";
    },
    onDocumentRevisionSubmissionAccepted: async () => {
      if (settings.cleanupFailure) return false;
      const saved = await readPendingSubmission(active);
      if (!saved?.wireSnapshot?.documentRevisionTarget || !saved.sourceDraftRevision) return false;
      return await drafts.clearAcceptedComposerDocumentRevisionDraft(active, { message: saved.message, target: saved.wireSnapshot.documentRevisionTarget, sourceDraftRevision: saved.sourceDraftRevision }) !== "unbound";
    },
    onSendInitSettled: (key, accepted) => { controller.settled.push(accepted); resolvers.current.get(key)?.(accepted); resolvers.current.delete(key); },
  });
  const send = (message: string, _attachments?: unknown, _revision?: string | null, context?: ComposerDraftSubmissionContext | null) => {
    controller.calls.push({ message, context: context ?? null });
    const key = `send-${++sequence.current}`;
    setIntent({ message, attachments: [], submissionWireSnapshot: context?.wireSnapshot, sendAttemptKey: key, sendTick: sequence.current });
    return new Promise<boolean>((resolve) => resolvers.current.set(key, resolve));
  };
  controller.recover = async () => {
    const saved = await readPendingSubmission(scope);
    if (!saved) throw new Error("Missing pending revision");
    setIntent({ message: saved.message, attachments: saved.attachments, submissionWireSnapshot: saved.wireSnapshot, sendAttemptKey: `recover-${++sequence.current}`, sendTick: sequence.current });
  };
  return <NextIntlClientProvider locale="en-US" messages={en}><main>
    <V2Composer key={settings.epoch} draftControlRef={control} referenceInputEnabled={false} draftPersistenceScope={active} onSend={send}
      onCaptureSubmissionWireSnapshot={(_action, _references, revisionTarget) => revisionTarget || !settings.otherCanvas ? wire : { ...wire, draftChatBinding: { draftId: "draft_other", version: 1, dirty: true } }} onResolveDraftSubmissionProtocol={async () => "terminal_v1"}
      running={settings.running} recoveryLocked={settings.locked} prefillText={settings.prefill} />
    <output id="turn">{runner.turnId ?? ""}</output>
  </main></NextIntlClientProvider>;
}
createRoot(document.getElementById("root")!).render(<StrictMode><AuthSessionProvider><Harness /></AuthSessionProvider></StrictMode>);

import { StrictMode, useRef, useState } from "react";
import type { AttachmentRef } from "../lib/types";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { AuthSessionProvider } from "../lib/auth/auth-session-context";
import { V2Composer, type ComposerDraftSubmissionContext, type ComposerDraftWriteProtocol } from "../components/chat/v2-composer";
import { useTurnRunner, type UseTurnRunnerArgs } from "../lib/use-turn-runner";
import * as drafts from "../lib/composer-ordinary-draft";
import { readPendingReferenceSubmission, type RecoveredSubmission } from "../lib/composer-submission-recovery";
import { composerDraftScopeSignature, type ComposerPersistenceScope } from "../lib/composer-persistence-scope";
import { stageComposerReferenceHandoff } from "../lib/composer-reference-handoff";
import { inputDocumentDisplayText, type InputDocument } from "../lib/composer-input-document";
import fixtures from "../../../contracts/chat-references/input-document-v1.json";
const referenceDraftInput = { input_document: fixtures.valid.find((item) => item.name === "exact_agent_and_parameterized_skill")!.normalized as InputDocument };
import zh from "../messages/zh-CN.json";
import en from "../messages/en-US.json";

const scope: ComposerPersistenceScope = { userId: "u_v2_reference", teamId: "team_ref", workspaceId: "ws_ref", requestedSessionId: null, authSessionGeneration: "auth_v2" };
type Settings = { enabled: boolean; protocol: ComposerDraftWriteProtocol; otherScope: boolean; steer: boolean; prefill: string | null; sessionTexts: string[]; cleanupFailure: boolean };
const controller = {
  calls: [] as { message: string; context: ComposerDraftSubmissionContext | null }[],
  pending: [] as RecoveredSubmission[], settled: [] as boolean[],
  configure: (_patch: Partial<Settings>) => {},
  read: async () => ({ draft: await drafts.readComposerInputDraft(scope), pending: await readPendingReferenceSubmission(scope) }),
  seed: async (document: InputDocument, attachments: AttachmentRef[] = []) => {
    const current = await drafts.readComposerInputDraft(scope);
    return drafts.writeComposerInputDraft(scope, { input_document: document, attachments }, current?.revision ?? null);
  },
  original: referenceDraftInput.input_document,
};
declare global { interface Window { referenceV2: typeof controller } }
window.referenceV2 = controller;
function Harness() {
  const [settings, setSettings] = useState<Settings>({ enabled: true, protocol: "legacy_v0", otherScope: false, steer: false, prefill: null, sessionTexts: [], cleanupFailure: false });
  const [intent, setIntent] = useState<Partial<UseTurnRunnerArgs>>({});
  const [recoveryLocked, setRecoveryLocked] = useState(false);
  const resolvers = useRef(new Map<string, (accepted: boolean) => void>());
  const activeScope = settings.otherScope ? { ...scope, workspaceId: "ws_other" } : scope;
  const sequence = useRef(0);
  controller.configure = (patch) => setSettings((previous) => ({ ...previous, ...patch }));
  const runner = useTurnRunner({
    ...intent, mode: "sse", workspaceId: activeScope.workspaceId,
    referenceSubmissionEnabled: settings.enabled,
    submissionProtocolEnabled: Boolean(intent.referenceInputDocument), submissionRecoveryScope: activeScope,
    currentChatScopeKey: composerDraftScopeSignature(activeScope),
    onSubmissionPending: (_attempt, _scope, pending) => { controller.pending.push(structuredClone(pending)); },
    onSubmissionCleared: () => setRecoveryLocked(false),
    onReferenceSubmissionAccepted: async () => {
      if (settings.cleanupFailure) return false;
      const saved = await readPendingReferenceSubmission(activeScope);
      if (!saved) return false;
      return await drafts.clearAcceptedComposerReferenceDraft(activeScope, {
        input_document: saved.input_document!, attachments: saved.attachments,
        sourceDraftRevision: saved.sourceDraftRevision,
      }) !== "unbound";
    },
    onSendInitSettled: (attempt, accepted) => { controller.settled.push(accepted); resolvers.current.get(attempt)?.(accepted); resolvers.current.delete(attempt); },
  });
  const send = (message: string, attachments: AttachmentRef[] | undefined, revision: string | null | undefined, context?: ComposerDraftSubmissionContext | null) => {
    controller.calls.push({ message, context: context ?? null });
    const key = `attempt-${++sequence.current}`;
    setIntent({ message, attachments: attachments ?? [], referenceSourceDraftRevision: revision ?? null, referenceInputDocument: context?.referenceInputDocument ?? null, sendAttemptKey: key, sendTick: sequence.current });
    return new Promise<boolean>((resolve) => resolvers.current.set(key, resolve));
  };
  const recover = async () => {
    const saved = await readPendingReferenceSubmission(scope);
    if (!saved) throw new Error("Missing saved submission");
    setRecoveryLocked(true);
    setIntent({ message: saved.message, attachments: saved.attachments, referenceSourceDraftRevision: saved.sourceDraftRevision, referenceInputDocument: saved.input_document, submissionWireSnapshot: saved.wireSnapshot, sendAttemptKey: `retry-${++sequence.current}`, sendTick: sequence.current });
  };
  return <NextIntlClientProvider locale={new URLSearchParams(location.search).get("locale") === "en" ? "en-US" : "zh-CN"} messages={new URLSearchParams(location.search).get("locale") === "en" ? en : zh}>
    <main><div className="test-composer">
      <V2Composer referenceInputEnabled={new URLSearchParams(location.search).has("auth") ? undefined : settings.enabled} draftPersistenceScope={activeScope} onSend={send}
        onResolveDraftSubmissionProtocol={async () => settings.protocol} running={settings.steer}
        onSteer={send} recoveryLocked={recoveryLocked} sessionTexts={settings.sessionTexts} prefillText={settings.prefill} />
    </div><button id="recover" onClick={() => void recover()}>Recover saved submission</button>
    <output id="turn">{runner.turnId ?? ""}</output><output id="error">{runner.sendErrorReason ?? ""}</output></main>
  </NextIntlClientProvider>;
}
async function mount() {
  const params = new URLSearchParams(location.search);
  if (params.get("seed") && !await drafts.readComposerInputDraft(scope)) await controller.seed(referenceDraftInput.input_document);
  if (params.get("handoff")) await stageComposerReferenceHandoff(scope, { schemaVersion: 2, input_document: referenceDraftInput.input_document, message: inputDocumentDisplayText(referenceDraftInput.input_document), attachments: [] });
  createRoot(document.getElementById("root")!).render(<StrictMode><AuthSessionProvider><Harness /></AuthSessionProvider></StrictMode>);
}
void mount();

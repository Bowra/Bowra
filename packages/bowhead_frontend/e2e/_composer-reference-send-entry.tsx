import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { ComposerReferenceEditor } from "../components/chat/composer-reference-editor";
import { useTurnRunner, type UseTurnRunnerArgs } from "../lib/use-turn-runner";
import {
  writeComposerReferenceDraft,
  readComposerReferenceDraft,
} from "../lib/composer-ordinary-draft";
import {
  readPendingReferenceSubmission,
  type RecoveredSubmission,
  type ComposerSubmissionWireSnapshot,
} from "../lib/composer-submission-recovery";
import {
  inputDocumentDisplayText,
  type InputDocument,
} from "../lib/composer-input-document";
import type { ComposerPersistenceScope } from "../lib/composer-persistence-scope";
import fixtures from "../../../contracts/chat-references/input-document-v1.json";
import zh from "../messages/zh-CN.json";

const originalDocument = fixtures.valid.find(
  (value) => value.name === "exact_agent_and_parameterized_skill",
)!.normalized as InputDocument;
const scope: ComposerPersistenceScope = {
  userId: "u_reference_send",
  teamId: "team_ref",
  workspaceId: "ws_ref",
  requestedSessionId: null,
  authSessionGeneration: "auth_ref_send",
};
const originalWire: ComposerSubmissionWireSnapshot = {
  composerSurface: "home",
  userProfile: null,
  preferredProvider: "original-provider",
  preferredModel: "original-model",
  planningMode: "off",
};
type Settings = {
  enabled: boolean;
  scopeKey: string;
  pendingMutation: boolean;
};
const controller = {
  pending: [] as RecoveredSubmission[],
  cleared: [] as string[],
  settled: [] as boolean[],
  configure: (_value: Partial<Settings>) => {},
  read: async () => ({
    draft: await readComposerReferenceDraft(scope),
    pending: await readPendingReferenceSubmission(scope),
  }),
};
declare global {
  interface Window {
    referenceSend: typeof controller;
  }
}
window.referenceSend = controller;
function Harness() {
  const [document, setDocument] = useState(originalDocument);
  const [revision, setRevision] = useState<string | null>(null);
  const [settings, setSettings] = useState<Settings>({
    enabled: true,
    scopeKey: "original-scope",
    pendingMutation: false,
  });
  const [intent, setIntent] = useState<Partial<UseTurnRunnerArgs>>({});
  controller.configure = (patch) =>
    setSettings((value) => ({ ...value, ...patch }));
  const runner = useTurnRunner({
    ...intent,
    mode: "sse",
    workspaceId: scope.workspaceId,
    submissionProtocolEnabled: true,
    submissionRecoveryScope: scope,
    referenceSubmissionEnabled: settings.enabled,
    currentChatScopeKey: settings.scopeKey,
    onSubmissionPending: (_attempt, _scope, pending) => {
      controller.pending.push(structuredClone(pending));
      if (settings.pendingMutation) {
        (
          pending as RecoveredSubmission & { input_document: InputDocument }
        ).input_document.nodes.splice(0);
        pending.wireSnapshot!.preferredModel = "mutated-callback-model";
      }
    },
    onSubmissionCleared: (_attempt, _scope, id) => controller.cleared.push(id),
    onSendInitSettled: (_attempt, accepted) =>
      controller.settled.push(accepted),
  });
  async function submit(value: InputDocument) {
    const written = await writeComposerReferenceDraft(
      scope,
      {
        schemaVersion: 2,
        input_document: value,
        message: inputDocumentDisplayText(value),
        attachments: [],
      },
      revision,
    );
    if (written.status !== "written") throw new Error("draft conflict");
    setRevision(written.revision);
    setIntent((previous) => ({
      message: inputDocumentDisplayText(value),
      referenceInputDocument: value,
      submissionWireSnapshot: originalWire,
      sendTick: (previous.sendTick ?? 0) + 1,
      sendAttemptKey: `attempt-${(previous.sendTick ?? 0) + 1}`,
    }));
  }
  async function recover() {
    const saved = await readPendingReferenceSubmission(scope);
    if (!saved?.wireSnapshot) throw new Error("missing original submission");
    setDocument(saved.input_document);
    setIntent((previous) => ({
      message: saved.message,
      referenceInputDocument: saved.input_document,
      submissionWireSnapshot: saved.wireSnapshot,
      sendTick: (previous.sendTick ?? 0) + 1,
      sendAttemptKey: `recovery-${(previous.sendTick ?? 0) + 1}`,
    }));
  }
  return (
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <main>
        <ComposerReferenceEditor
          scope={scope}
          snapshot={{ scope, resetKey: "original", document }}
          onChange={setDocument}
          onSubmitIntent={(value) => void submit(value)}
        />
        <button id="recover" onClick={() => void recover()}>
          Retry saved submission
        </button>
        <output id="turn">{runner.turnId ?? ""}</output>
        <output id="error">{runner.sendErrorReason ?? ""}</output>
      </main>
    </NextIntlClientProvider>
  );
}
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
);

import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import {
  ComposerReferenceEditor,
  type ComposerReferenceEditorSnapshot,
} from "../components/chat/composer-reference-editor";
import {
  inputDocumentFromText,
  type InputDocument,
} from "../lib/composer-input-document";
import type { ComposerPersistenceScope } from "../lib/composer-persistence-scope";
import en from "../messages/en-US.json";
import zh from "../messages/zh-CN.json";

type State = {
  scope: ComposerPersistenceScope;
  snapshot: ComposerReferenceEditorSnapshot;
  locale: "zh-CN" | "en-US";
  readOnly: boolean;
  entryMode?: "ordinary" | "steer";
};
declare global {
  interface Window {
    referenceComponent: {
      changes: InputDocument[];
      submits: InputDocument[];
      ghostKeys: string[];
      update: (patch: Partial<State>) => void;
    };
  }
}
const scope: ComposerPersistenceScope = {
  userId: "u_test",
  teamId: "t_test",
  workspaceId: "ws_test",
  requestedSessionId: null,
  authSessionGeneration: "auth_test",
};
const controller = {
  changes: [] as InputDocument[],
  submits: [] as InputDocument[],
  ghostKeys: [] as string[],
  update: (_patch: Partial<State>) => {},
};
window.referenceComponent = controller;
function Harness() {
  const [state, setState] = useState<State>({
    scope,
    snapshot: {
      scope,
      resetKey: "initial",
      document: inputDocumentFromText(""),
    },
    locale: "zh-CN",
    readOnly: false,
  });
  controller.update = (patch) => setState((value) => ({ ...value, ...patch }));
  return (
    <NextIntlClientProvider
      locale={state.locale}
      messages={state.locale === "zh-CN" ? zh : en}
    >
      <main>
        <h1>Reference component contract</h1>
        <div className="test-composer">
          <button type="button" id="before">
            Before input
          </button>
          <ComposerReferenceEditor
            {...state}
            onChange={(value) => controller.changes.push(value)}
            onSubmitIntent={(value) => controller.submits.push(value)}
            onGhostKeyDown={(event) => {
              controller.ghostKeys.push(event.key);
              return false;
            }}
          />
          <button type="button" id="after">
            After input
          </button>
        </div>
      </main>
    </NextIntlClientProvider>
  );
}
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
);

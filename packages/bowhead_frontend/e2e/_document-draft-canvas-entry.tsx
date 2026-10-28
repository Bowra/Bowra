import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import {
  DocumentDraftCanvasObject,
  type DocumentDraftCanvasPublication,
} from "../components/canvas/document-draft-canvas-object";
import fixture from "../__tests__/fixtures/document-draft-canvas.json";
import en from "../messages/en-US.json";
import "../styles/ui-refresh-cards.css";

// Test-only lifecycle harness. Root navigation and Canvas registration remain separate.
function Fixture() {
  const [open, setOpen] = useState(true);
  const [generation, setGeneration] = useState(1);
  const [refreshKey, setRefreshKey] = useState(0);
  const [selected, setSelected] = useState(true);
  const [receipt, setReceipt] = useState<DocumentDraftCanvasPublication | null>(
    null,
  );
  const context = { ...fixture.context, instanceId: `browser-${generation}` };
  const live = useRef({ open, context });
  live.current = { open, context };
  return (
    <main style={{ maxWidth: 760, margin: "auto" }}>
      <label>
        Unsent input{" "}
        <input aria-label="Unsent input" defaultValue="Keep my input" />
      </label>
      <div>
        <button onClick={() => setOpen(false)}>Close view</button>
        <button
          onClick={() => {
            setGeneration((value) => value + 1);
            setOpen(true);
          }}
        >
          Reopen view
        </button>
        <button onClick={() => setRefreshKey((value) => value + 1)}>
          Refresh view
        </button>
        <button onClick={() => setSelected((value) => !value)}>
          Switch view
        </button>
      </div>
      <output aria-label="Published source">
        {receipt?.source.artifactId ?? "No publication"}
      </output>
      <div hidden={!selected}>
        {open ? (
          <DocumentDraftCanvasObject
            context={context}
            refreshKey={refreshKey}
            isCurrent={(original) =>
              live.current.open &&
              original.instanceId === live.current.context.instanceId
            }
            onPublished={setReceipt}
          />
        ) : null}
      </div>
      {!selected ? <p>Another object is selected</p> : null}
    </main>
  );
}
createRoot(document.getElementById("root")!).render(
  <NextIntlClientProvider locale="en-US" messages={en}>
    <Fixture />
  </NextIntlClientProvider>,
);

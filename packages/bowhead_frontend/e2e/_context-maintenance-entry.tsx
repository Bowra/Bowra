import "./_process-shim";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { SessionTopbarMenu } from "../components/chat/session-topbar-menu";
import { useContextMaintenance } from "../lib/use-context-maintenance";
import en from "../messages/en-US.json";
import "../styles/ui-refresh-workbench.css";
import "../styles/ui-refresh-chrome.css";

function Maintenance() {
  const [message, setMessage] = useState("");
  const [attached, setAttached] = useState("");
  const maintenance = useContextMaintenance({
    scope: { userId: "u_e2e", teamId: "team_test", workspaceId: "ws_test", requestedSessionId: "s_context", authSessionGeneration: "auth-e2e" },
    blocked: false,
    attach: (receipt) => { setAttached(receipt.turn_id); return true; },
    onMessage: setMessage,
  });
  return <main style={{ maxWidth: 640, margin: "0 auto" }}>
    <header className="v2-mobile-topbar-right" style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
      <h1>Quarterly analysis</h1>
      <SessionTopbarMenu sessionId="s_context" title="Quarterly analysis" onCompactContext={maintenance.run}
        compactDisabledReason={maintenance.busy ? "Organizing context…" : null} />
    </header>
    <label>Unsent question<textarea aria-label="Unsent question" defaultValue="Keep my draft" /></label>
    <p role="status" aria-label="Maintenance status">{message}</p>
    <output id="attached-turn">{attached}</output>
  </main>;
}
createRoot(document.getElementById("root")!).render(<NextIntlClientProvider locale="en-US" messages={en}><Maintenance /></NextIntlClientProvider>);

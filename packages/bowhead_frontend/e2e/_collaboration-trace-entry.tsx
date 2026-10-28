import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { AgentTrace } from "../components/chat/step-list/AgentTrace";
import { CollaborationTraceProvider } from "../components/chat/step-list/collaboration-trace-state";
import { dispatchToTurnStore } from "../lib/turn-store-dispatch";
import { replayTurnEventRows } from "../lib/turn-store-replay";
import en from "../messages/en-US.json";
import zh from "../messages/zh-CN.json";

const locale = new URLSearchParams(location.search).get("locale") === "zh-CN" ? "zh-CN" : "en-US";
dispatchToTurnStore("agent.collaboration", { card_id: "turn-browser", collaboration_revision: 1 });
if (new URLSearchParams(location.search).get("history") === "cancelled") {
  replayTurnEventRows("turn-browser", [{ seq: 2, event_type: "done", occurred_at: "", payload: { turn_id: "turn-browser", status: "cancelled", seq: 2 } }]);
}
createRoot(document.getElementById("root")!).render(
  <NextIntlClientProvider locale={locale} messages={locale === "zh-CN" ? zh : en}>
    <main style={{ maxWidth: 720, margin: "0 auto" }}>
      <h1>Collaboration trace</h1>
      <div className="bh-step-list"><CollaborationTraceProvider turnId="turn-browser">
        <AgentTrace steps={[]} peerNames={{}} peerProse={{}} />
      </CollaborationTraceProvider></div>
      <label>Draft <input defaultValue="Keep my draft" /></label>
      <button type="button" onClick={() => dispatchToTurnStore("agent.collaboration", { card_id: "turn-browser", collaboration_revision: 2 })}>Publish reply verification</button>
      <button type="button" onClick={() => dispatchToTurnStore("done", { turn_id: "turn-browser", status: "cancelled", seq: 2 })}>Confirm stop</button>
    </main>
  </NextIntlClientProvider>,
);

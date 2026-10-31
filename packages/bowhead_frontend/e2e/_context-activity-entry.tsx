import "./_process-shim";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { dispatchToTurnStore } from "../lib/turn-store-dispatch";
import { ContextActivityProvider } from "../components/chat/step-list/ContextActivity";
import { AgentTrace } from "../components/chat/step-list/AgentTrace";
import { RunningBand } from "../components/chat/step-list/RunningBand";
import { TurnRunningProvider } from "../components/chat/step-list/turn-running";
import en from "../messages/en-US.json";
import zh from "../messages/zh-CN.json";
import "../styles/ui-refresh-workbench.css";

const search = new URLSearchParams(location.search);
const locale = search.get("locale") === "zh-CN" ? "zh-CN" : "en-US";
document.documentElement.lang = locale;
document.documentElement.dataset.theme = search.get("theme") ?? "light";
const turnId = "context-activity-fixture";
const event = {
  card_id: turnId, turn_id: turnId, schema_version: 1, operation_id: `mco_${"a".repeat(32)}`,
  execution_id: "execution-child", lane: "ordinary", subagent_id: "sub_child", profile: "compute",
  phase: "running", trigger: "threshold", source_generation: `mcg_${"b".repeat(32)}`, generation_id: null,
};
const completed = { ...event, phase: "committed", generation_id: `mcg_${"c".repeat(32)}` };
const replay = search.get("phase") === "committed";
dispatchToTurnStore("agent.context", { ...(replay ? completed : event), seq: replay ? 2 : 1 });

function Activity() {
  const [running, setRunning] = useState(!replay);
  return <NextIntlClientProvider locale={locale} messages={locale === "zh-CN" ? zh : en}>
    <main style={{ maxWidth: 720, margin: "0 auto" }}>
      <TurnRunningProvider running={running}>
        <ContextActivityProvider turnId={turnId}>
          {running ? <RunningBand steps={[]} running onExpand={() => {}} /> : null}
          <div className="bh-step-list"><AgentTrace steps={[]} peerNames={{ sub_child: "compute" }} peerProse={{}} /></div>
        </ContextActivityProvider>
      </TurnRunningProvider>
      <button type="button" id="complete-context" onClick={() => {
        dispatchToTurnStore("agent.context", { ...completed, seq: 2 });
        setRunning(false);
      }}>Complete activity</button>
    </main>
  </NextIntlClientProvider>;
}
createRoot(document.getElementById("root")!).render(<Activity />);

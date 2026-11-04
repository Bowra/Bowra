import "./_process-shim";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { Chart as ChartJS } from "chart.js";
import { StepSummary } from "../components/chat/step-list/StepSummary";
import { AgentTrace } from "../components/chat/step-list/AgentTrace";
import { ClarificationCard } from "../components/chat/step-list/ClarificationCard";
import { stepToStepView } from "../components/chat/step-list/step-view";
import { ReportView } from "../components/canvas/report-view";
import { dispatchToTurnStore } from "../lib/turn-store-dispatch";
import { replayTurnEventRows } from "../lib/turn-store-replay";
import { useTurn } from "../lib/turn-store";
import type { ReportSection } from "../lib/types";
import type { TurnEventDTO } from "../lib/api";
import { ChartThemePreviewProvider, chartThemeBaseForMode, resolveChartThemeForStyle } from "../lib/chart-theme";
import evidence from "./fixtures/tool-outcomes.json";
import en from "../messages/en-US.json";
import zh from "../messages/zh-CN.json";
import "../styles/report-canvas.css";
import "../styles/ui-refresh-canvas.css";

const query = new URLSearchParams(location.search);
const locale = query.get("locale") === "zh-CN" ? "zh-CN" : "en-US";
if (query.get("history") === "replay") {
  replayTurnEventRows("t_test", evidence.replay as unknown as TurnEventDTO[]);
} else {
  evidence.live.forEach((event) => dispatchToTurnStore(event.event_type, event.payload));
}
Object.assign(window, { toolOutcomeChartLabels: () => [...document.querySelectorAll("canvas")].flatMap((canvas) => ChartJS.getChart(canvas)?.data.labels ?? []) });

function Host() {
  const turn = useTurn("t_test");
  const [submitted, setSubmitted] = useState<string[]>([]);
  return <NextIntlClientProvider locale={locale} messages={locale === "zh-CN" ? zh : en}>
    <ChartThemePreviewProvider theme={resolveChartThemeForStyle("bowhead-editorial", { contrast: "standard", colorVision: "standard" }, chartThemeBaseForMode("light"), "light")}>
      <main style={{ maxWidth: 800, margin: "0 auto" }}>
        <h1>Synthetic session regression: fixed source and shared failures</h1>
        <StepSummary steps={(turn?.steps ?? []).map((step) => stepToStepView(step, {}))} stepOutcome={turn?.stepOutcome} onExpand={() => {}} />
        <p role="note">{evidence.delivery.caveats.join(" ")}</p>
        <div className="bh-step-list"><AgentTrace steps={(turn?.steps ?? []).map((step) => stepToStepView(step, {}))} peerNames={{}} peerProse={{}} /></div>
        <ClarificationCard questions={[{ question: "Analysis scope", options: ["Revenue", "Profit", "Brands", "Stores", "Members", "Traffic"], type: "multi_select" }]} onSubmitSingle={() => {}} onSubmitMulti={({ values }) => setSubmitted(values)} />
        <output aria-label="Submitted scope">{submitted.join(", ")}</output>
        <ReportView sections={[evidence.section as unknown as ReportSection]} canvasId="cv_test" readOnly seqAuthoritative />
      </main>
    </ChartThemePreviewProvider>
  </NextIntlClientProvider>;
}
createRoot(document.getElementById("root")!).render(<Host />);

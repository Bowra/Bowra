import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "@/lib/turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { replayTurnEventRows } from "@/lib/turn-store-replay";
import { CANVAS_FOCUS_DATA_EVENT } from "@/lib/canvas-workspace-focus";
import { AgentTrace } from "./AgentTrace";
import { buildBandModel } from "./running-band-model";
import { stepsToStepViews } from "./step-view";
import type { StepView } from "./types";

afterEach(() => { cleanup(); _resetAllTurnsForTesting(); _resetNarrativeCursorsForTesting(); });

function step(id: string, over: Partial<StepView> = {}): StepView {
  return { stepId: id, parentStepId: null, concurrencyGroupId: null, stepType: "tool_run", headerZh: "", toolName: "bi_query", state: "done", ...over };
}
const query = step("query", { label: "Required revenue", summary: "1 row", detail: { kind: "table", cols: ["Revenue"], rows: [[4200]] }, outputArtifacts: [{ artifactId: "art_revenue", filename: "revenue.csv", dataRole: "structured" }, { artifactId: "art_log", filename: "run.log", dataRole: "stdout" }] });
const calculation = step("calculation", { toolName: "python_sandbox", label: "Check retained data", summary: "Calculation retained" });
function trace(steps: StepView[]) { return <AgentTrace steps={steps} peerNames={{}} peerProse={{}} />; }

describe.each(["subagent_return", "review_delivery"] as const)("Result-focused execution history for %s", (toolName) => {
  function correction(id = "review"): StepView {
    return step(id, { toolName, state: "failed", error: { kind: "permanent", message: "", code: "tool_delivery_review_required", retryable: false } });
  }
  const accepted = step("return", { toolName, detail: { kind: "stdout", text: '{"delivery_review":{"requirements":[],"issues":[]}}' } });
  it("retains the full business flow while omitting internal failures and successful packing", () => {
    const records = [query, calculation, correction(), correction("second-review"), accepted];
    const original = structuredClone(records);
    const { container } = renderIntl(trace(records));
    const rows = [...container.querySelectorAll(".stp-head")];
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("Required revenue");
    expect(rows[1]?.textContent).toContain("Check retained data");
    fireEvent.click(rows[0]!);
    expect(screen.getByText("4,200")).toBeVisible();
    const focus = vi.fn();
    window.addEventListener(CANVAS_FOCUS_DATA_EVENT, focus);
    try {
      fireEvent.click(screen.getByRole("button", { name: /revenue.csv/ }));
      expect(focus).toHaveBeenCalledOnce();
      expect((focus.mock.calls[0][0] as CustomEvent).detail).toEqual({ artifactId: "art_revenue", entryPoint: "chat" });
    } finally {
      window.removeEventListener(CANVAS_FOCUS_DATA_EVENT, focus);
    }
    expect(screen.getByRole("link", { name: /run.log/ })).toHaveAttribute("href", "/api/v1/files/art_log");
    expect(screen.queryByText("整理结果")).toBeNull();
    expect(container.textContent).not.toContain("delivery_review");
    expect(screen.queryByText(/交付尚未核实|技术记录|核对记录/)).toBeNull();
    expect(container.querySelector(".agb-head .g-part")).toBeNull();
    expect(records).toEqual(original);
  });

  it("omits internal correction history while preserving actual current activity", () => {
    const current = { ...accepted, stepId: "packing", state: "active" as const };
    const records = [query, calculation, correction(), current];
    const band = buildBandModel({ steps: records });
    expect(band.issueCount).toBe(0);
    expect(band.history.map((row) => row.key)).toEqual(["query", "calculation"]);
    expect(band.current).toBeTruthy();
    const original = records.map((record) => record.state);
    expect(original).toEqual(["done", "done", "failed", "active"]);
  });

  it("does not replace an internal-only Lead trace with an empty card", () => {
    const { container } = renderIntl(trace([correction(), accepted]));
    expect(container.querySelector(".agb")).toBeNull();
  });

  it("preserves a real partial closeout without displaying its internal packing row", () => {
    const partial = { ...accepted, state: "partial" as const };
    const { container } = renderIntl(trace([query, correction(), partial]));
    expect(container.querySelectorAll(".stp")).toHaveLength(1);
    expect(container.querySelector(".agb-head .g-part")).toBeTruthy();
    expect(screen.queryByText("整理结果")).toBeNull();
  });

  it.each(["succeeded_with_warnings", "partial"] as const)("keeps the canonical Peer outcome %s with all business steps", (status) => {
    const records = [query, calculation, correction(), { ...accepted, state: status === "partial" ? "partial" as const : "done" as const }]
      .map((s) => ({ ...s, subagentId: "analyst" }));
    const { container } = renderIntl(<AgentTrace steps={records} peerNames={{ analyst: "compute" }} peerProse={{}} peerTerminalById={{ analyst: { status } }} />);
    fireEvent.click(screen.getByRole("button", { name: /数据与计算分析师/ }));
    expect(container.querySelectorAll(".stp")).toHaveLength(2);
    expect(screen.getByText("Required revenue")).toBeVisible();
    expect(container.querySelector(`.agb-head .g-${status === "partial" ? "part" : "done"}`)).toBeTruthy();
    expect(screen.queryByText("整理结果")).toBeNull();
  });

  it("does not hide a business failure carrying the control-error code", () => {
    const business = { ...correction(), toolName: "python_sandbox" as const };
    const { container } = renderIntl(trace([business, accepted]));
    expect(container.querySelector(".stp.is-fail .stp-detail")).toBeTruthy();
    expect(screen.getByText(/交付尚未核实/)).toBeVisible();
  });

  it.each(["tool_execution_unknown", "source_access_denied", "provider_unavailable"])("retains exceptional return outcome %s", (code) => {
    const failure = correction(); failure.error = { ...failure.error!, code };
    const { container } = renderIntl(trace([failure, accepted]));
    expect(container.querySelector(".stp.is-fail .stp-detail")).toBeTruthy();
    expect(container.querySelector(".agb-head .g-part")).toBeTruthy();
    expect(container.querySelector(".stp-tool")?.textContent).toContain("整理结果");
  });


  it("keeps business argument failures visible while omitting native corrections", () => {
    const native = { ...correction(), error: { kind: "permanent" as const, message: "Private repair", code: "tool_arguments_invalid" } };
    const business = { ...native, stepId: "business", toolName: "python_sandbox" as const };
    const { container } = renderIntl(trace([native, business, accepted]));
    expect(container.querySelectorAll(".stp")).toHaveLength(1);
    expect(container.querySelector(".bh-step-failure")).toBeTruthy();
    expect(container.textContent).not.toContain("Private repair");
    const band = buildBandModel({ steps: [native, query] });
    expect(band.issueCount).toBe(0);
  });

  it("keeps unknown and cancelled return records visible", () => {
    const unknown = correction(); unknown.error = { ...unknown.error!, kind: "unknown" };
    const cancelled = { ...accepted, stepId: "stopped", state: "cancelled" as const };
    const { container } = renderIntl(trace([unknown, cancelled]));
    expect(container.querySelectorAll(".stp")).toHaveLength(2);
    expect(screen.getByText("(已停止)")).toBeVisible();
  });

  it.each(["review", "arguments", "legacy_arguments"])("uses the same visibility after live ingestion and persisted replay without deleting receipts: %s", (reason) => {
    const turn = "t_closeout_display";
    const wire = [
      ["step_list_card_created", { plan: [] }],
      ["step_started", { step_id: "query", step_type: "tool_run", tool_name: "bi_query", header_zh: "Query", started_at: "2026-11-02T10:00:00Z" }],
      ["step_done", { step_id: "query", state: "failure", duration_ms: 100, sub_steps: [{ id: "first", label: "First expected refusal", status: "failed" }, { id: "second", label: "Second expected refusal", status: "failed" }], error: { code: "tool_execution_failed", message_zh: "", is_transient: false } }],
      ["step_started", { step_id: "review", step_type: "tool_run", tool_name: toolName, header_zh: "Pack result", started_at: "2026-11-02T10:00:01Z" }],
      ["step_done", { step_id: "review", state: "failure", duration_ms: 10, error: { code: reason === "review" ? "tool_delivery_review_required" : reason === "arguments" ? "tool_arguments_invalid" : "tool_execution_failed", type: reason === "review" ? "tool_exception" : "tool_arguments_invalid", message_zh: "Private model repair instruction", is_transient: false } }],
      ["step_started", { step_id: "accepted", step_type: "tool_run", tool_name: toolName, header_zh: "Pack result", started_at: "2026-11-02T10:00:02Z" }],
      ["step_done", { step_id: "accepted", state: "success", duration_ms: 10 }],
      ["final_answer", { text: "The requested result was delivered." }],
      ["done", { status: "completed" }],
    ] as const;
    const rows = wire.map(([event_type, payload], seq) => ({ event_type, seq: seq + 1, payload: { ...payload, card_id: turn, turn_id: turn, seq: seq + 1 }, occurred_at: "" }));
    for (const row of rows) dispatchToTurnStore(row.event_type, row.payload);
    const live = getTurnSnapshot(turn)!;
    const rendered = renderIntl(trace(stepsToStepViews(live.steps, live.peerTerminalById)));
    expect(rendered.container.querySelector(".bh-step-failure")).toBeNull();
    if (!screen.queryByText("First expected refusal")) fireEvent.click(rendered.container.querySelector(".stp-head")!);
    expect(screen.getByText("First expected refusal")).toBeVisible();
    const liveText = rendered.container.textContent;
    expect(rendered.container.querySelectorAll(".stp")).toHaveLength(1);
    expect(live.steps).toHaveLength(3);
    const stored = structuredClone(live.steps);
    cleanup(); _resetAllTurnsForTesting(); _resetNarrativeCursorsForTesting();
    replayTurnEventRows(turn, rows);
    const replay = getTurnSnapshot(turn)!;
    const replayed = renderIntl(trace(stepsToStepViews(replay.steps, replay.peerTerminalById)));
    if (!screen.queryByText("First expected refusal")) fireEvent.click(replayed.container.querySelector(".stp-head")!);
    expect(replayed.container.textContent).toBe(liveText);
    expect(replay.steps).toEqual(stored);
    expect(replayed.container.querySelectorAll(".stp")).toHaveLength(1);
  });
});

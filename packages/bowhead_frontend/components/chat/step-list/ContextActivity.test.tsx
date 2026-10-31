import { act, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllTurnsForTesting, getTurnSnapshot, hasPeerDisclosureFacts } from "@/lib/turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { ContextActivityProvider, ContextActivityRows } from "./ContextActivity";
import { TurnRunningProvider } from "./turn-running";
import { AgentTrace } from "./AgentTrace";

const turnId = "context-turn";
const started = {
  card_id: turnId, turn_id: turnId, schema_version: 1, operation_id: `mco_${"a".repeat(32)}`,
  execution_id: "execution-child", lane: "ordinary", subagent_id: "sub_child", profile: "compute",
  phase: "running", trigger: "threshold", source_generation: `mcg_${"b".repeat(32)}`, generation_id: null,
};
const completed = { ...started, phase: "committed", generation_id: `mcg_${"c".repeat(32)}` };

afterEach(() => { _resetAllTurnsForTesting(); _resetNarrativeCursorsForTesting(); });

describe("context maintenance activity", () => {
  it("deduplicates replay, keeps terminal truth and belongs to the child without a Tool or answer", () => {
    dispatchToTurnStore("agent.context", { ...started, seq: 1 });
    renderIntl(<TurnRunningProvider running><ContextActivityProvider turnId={turnId}>
      <AgentTrace steps={[]} peerNames={{ sub_child: "compute" }} peerProse={{}} />
    </ContextActivityProvider></TurnRunningProvider>, { locale: "en-US" });
    expect(screen.getByText("Organizing context…").closest(".agb")).toBeTruthy();
    act(() => { dispatchToTurnStore("agent.context", { ...completed, seq: 2 }); });
    act(() => { dispatchToTurnStore("agent.context", { ...started, seq: 1 }); });
    act(() => { dispatchToTurnStore("agent.context", { ...completed, seq: 3 }); });
    expect(screen.getAllByText("Context organized")).toHaveLength(1);
    expect(screen.queryByText("Organizing context…")).toBeNull();
    const state = getTurnSnapshot(turnId);
    expect(hasPeerDisclosureFacts(state)).toBe(true);
    expect(state?.steps).toHaveLength(0);
    expect(state?.cardFinalized).toBe(false);
  });

  it("replays a completed root operation without inventing a user answer", () => {
    dispatchToTurnStore("agent.context", { ...completed, subagent_id: null, profile: "lead", seq: 2 });
    renderIntl(<ContextActivityProvider turnId={turnId}><AgentTrace steps={[]} peerNames={{}} peerProse={{}} /></ContextActivityProvider>, { locale: "zh-CN" });
    expect(screen.getByText("上下文已整理")).toBeTruthy();
  });

  it("does not leave a spinner on an ended Turn with only a saved start", () => {
    dispatchToTurnStore("agent.context", { ...started, seq: 1 });
    renderIntl(<ContextActivityProvider turnId={turnId}><ContextActivityRows subagentId="sub_child" /></ContextActivityProvider>, { locale: "en-US" });
    expect(screen.getByText("Context organization interrupted")).toBeTruthy();
    expect(document.querySelector(".ag-context-activity .spin")).toBeNull();
  });

  it("ignores malformed and cross-owner events before they can alter maintenance truth", () => {
    dispatchToTurnStore("agent.context", { ...started, schema_version: 2, seq: 1 });
    expect(getTurnSnapshot(turnId)?.contextActivities).toBeUndefined();
    dispatchToTurnStore("agent.context", { ...started, seq: 2 });
    dispatchToTurnStore("agent.context", { ...completed, execution_id: "execution-other", seq: 3 });
    expect(getTurnSnapshot(turnId)?.contextActivities?.[started.operation_id].phase).toBe("running");
  });
});

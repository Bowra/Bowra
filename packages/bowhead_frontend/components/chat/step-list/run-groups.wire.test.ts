// @vitest-environment jsdom
/**
 * The run groups from events in their wire shape. Step times arrive as `…+00:00#1382` (`utc_now_iso`); 116, 1.7.77:
 * read as Invalid Date, two helpers dispatched 0.5 ms apart could not be seen to overlap, so the second read
 * "第 2 次", and no step ever showed its own time.
 */
import { afterEach, describe, expect, it } from "vitest";

import { _resetAllTurnsForTesting, getTurnSnapshot } from "@/lib/turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "@/lib/turn-store-dispatch";

import { buildRunGroups } from "./run-groups";
import { stepsToStepViews } from "./step-view";

const T = "t_wire";
let seq = 0;
const send = (event: string, payload: Record<string, unknown>) => {
  seq += 1;
  dispatchToTurnStore(event, { seq, card_id: T, turn_id: T, ...payload });
};

afterEach(() => { _resetAllTurnsForTesting(); _resetNarrativeCursorsForTesting(); seq = 0; });

describe("run groups from wire events", () => {
  it("tells parallel helpers apart by task when their dispatch times carry the server suffix", () => {
    send("turn_started", { session_id: "s_1", started_at: "2026-11-26T02:05:04.812323+00:00" });
    send("step_list_card_created", {});
    for (const [step, peer, task, at] of [
      ["spawn_a", "sub_a", "销售额前 5", "2026-11-26T02:05:13.718756+00:00#1382"],
      ["spawn_b", "sub_b", "客流量前 5", "2026-11-26T02:05:13.719232+00:00#1384"],
    ]) {
      send("step_started", { step_id: step, step_type: "subagent_spawn", tool_name: "subagent_spawn", header_zh: "派发", input_preview: task, started_at: at });
      send("peer_dispatched", { spawn_id: peer, subagent_id: peer, parent_step_id: step });
      send("agent_spawned", { subagent_id: peer, profile: "compute", task });
    }
    send("lead.subagent_summary", { spawn_id: "sub_b", subagent_id: "sub_b", parent_step_id: "spawn_b", status: "succeeded", duration_ms: 19084 });

    const state = getTurnSnapshot(T)!;
    const steps = stepsToStepViews(state.steps, state.peerTerminalById);
    // The server's times, not the local receipt time an unreadable value falls back to.
    expect(steps.map((step) => step.startedAtMs)).toEqual([Date.UTC(2026, 10, 26, 2, 5, 13, 718), Date.UTC(2026, 10, 26, 2, 5, 13, 719)]);
    const groups = buildRunGroups({ steps, peerNames: state.peerNames, peerTerminalById: state.peerTerminalById, peerTasks: state.peerTasks });
    expect(groups.map((group) => [group.task, group.repeat])).toEqual([["销售额前 5", null], ["客流量前 5", null]]);
  });
});

/**
 * Turn visibility D7/FR5: code a helper is writing shows under that helper, worded with the purpose the model
 * gave it, in a code box tagged with the declared language that follows the newest line. The running band names
 * the purpose and shows the newest line. Live and replayed events project to the same rows (chat invariant).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import type { TurnEventDTO } from "@/lib/api";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "@/lib/turn-store";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { replayTurnEventRows } from "@/lib/turn-store-replay";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

import { AgentTrace } from "./AgentTrace";
import { buildBandModel } from "./running-band-model";
import { stepsToStepViews } from "./step-view";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
});

type Ev = [string, Record<string, unknown>];

const INTENT = "生成门店看板";
const CODE = Array.from({ length: 14 }, (_, i) => `print("row ${String(i + 1).padStart(2, "0")}")`).join("\n");

/** A helper writing code: the arguments stream in two pieces, the closing quote not yet sent. */
function helperWriting(turnId: string, { withCode = true } = {}): Ev[] {
  const args = JSON.stringify({ intent: INTENT, code: CODE });
  const streamed = withCode ? args.slice(0, -2) : args.slice(0, args.indexOf(',"code"'));
  let seq = 0;
  const e = (name: string, payload: Record<string, unknown>): Ev => [name, { card_id: turnId, evt_seq: ++seq, ...payload }];
  return [
    e("step_list_card_created", {}),
    e("step_started", {
      step_id: "writing_c1",
      step_type: "lead_thinking_writing",
      header_zh: "写代码",
      started_at: "2026-11-22T08:00:00.000Z",
      tool_name: "python_sandbox",
      call_id: "c1",
      code_language: "python",
      subagent_id: "peer_compute",
    }),
    e("step_progress", { step_id: "writing_c1", content_delta: streamed.slice(0, 30) }),
    e("step_progress", { step_id: "writing_c1", content_delta: streamed.slice(30) }),
  ];
}

function views(turnId: string) {
  const st = getTurnSnapshot(turnId);
  return st ? stepsToStepViews(st.steps, st.peerTerminalById) : [];
}

function feedLive(events: Ev[]) {
  for (const [name, payload] of events) dispatchToTurnStore(name, payload);
}

/** The helper's block (opened if folded) and its writing row (opened if folded). */
function openHelperRow(container: HTMLElement): HTMLElement {
  const block = container.querySelector(".agb") as HTMLElement;
  expect(block.querySelector(".agb-name")?.textContent).toBeTruthy();
  if (!block.querySelector(".stp")) fireEvent.click(block.querySelector(".agb-head") as HTMLElement);
  const row = block.querySelector(".stp") as HTMLElement;
  if (!row.querySelector(".stp-detail")) fireEvent.click(row.querySelector(".stp-head") as HTMLElement);
  return row;
}

function toRows(events: Ev[]): TurnEventDTO[] {
  return events.map(([name, payload], i) => ({ seq: i + 1, event_type: name, payload, occurred_at: "2026-11-22T08:00:00.000Z" }));
}

describe("live code", () => {
  it("projects a helper's code the same live and replayed, attributed to the helper and named by its purpose", () => {
    feedLive(helperWriting("t-live"));
    replayTurnEventRows("t-replay", toRows(helperWriting("t-replay")));
    const [live] = views("t-live");
    expect(live).toMatchObject({ stepType: "lead_thinking_writing", subagentId: "peer_compute", label: INTENT, codeLanguage: "python" });
    expect(views("t-replay")).toEqual(views("t-live"));
  });

  it("shows the last twelve lines with the language tag and no controls", () => {
    feedLive(helperWriting("t-box"));
    const { container } = render(<AgentTrace steps={views("t-box")} peerNames={{ peer_compute: "compute" }} peerProse={{}} />);
    const row = openHelperRow(container);
    const box = row.querySelector(".codewrap") as HTMLElement;
    expect(box.querySelector(".lang")?.textContent).toBe("Python");
    const text = box.textContent ?? "";
    expect(text).toContain('print("row 14")');
    expect(text).toContain('print("row 03")');
    expect(text).not.toContain('print("row 02")');
    expect(box.querySelector("button")).toBeNull();
  });

  it("keeps the box twelve lines tall while the code grows", () => {
    // User request on an observed deployment: the box grew line by line and jumped on wrapped long lines. The height lives in CSS
    // (`.codewrap.is-fixed`), so this checks the box declares it. Mutation: drop `fixedLines` → this test fails.
    feedLive(helperWriting("t-fixed"));
    const { container } = render(<AgentTrace steps={views("t-fixed")} peerNames={{ peer_compute: "compute" }} peerProse={{}} />);
    const box = openHelperRow(container).querySelector(".codewrap") as HTMLElement;
    expect(box.classList.contains("is-fixed")).toBe(true);
    expect(box.style.getPropertyValue("--code-lines")).toBe("12");
  });

  it("shows the purpose but no box before the code starts, never the arguments' JSON", () => {
    feedLive(helperWriting("t-early", { withCode: false }));
    const [view] = views("t-early");
    expect(view!.label).toBe(INTENT);
    const { container } = render(<AgentTrace steps={[view!]} peerNames={{ peer_compute: "compute" }} peerProse={{}} />);
    const row = openHelperRow(container);
    expect(row.querySelector(".codewrap")).toBeNull();
    expect(row.textContent ?? "").not.toContain('{"intent"');
  });

  it("puts the purpose in the band's action and the newest code line in its foot", () => {
    feedLive(helperWriting("t-band"));
    const model = buildBandModel({ steps: views("t-band"), peerNames: { peer_compute: "compute" } });
    expect(model.current?.what.endsWith(` · ${INTENT}`)).toBe(true);
    expect(model.current?.prose).toBe('print("row 14")');
    expect(model.current?.proseIsCode).toBe(true);
    expect(model.current?.owner).toBe("peer_compute");
  });
});

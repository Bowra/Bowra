import { afterEach, expect, it, vi } from "vitest";
import { captureCoachCanvasBinding, parseCoachCanvasBinding, type CoachCanvasBinding, type CoachEditorSnapshot } from "./coach-canvas-target";
import { dispatchToTurnStore } from "./turn-store-dispatch";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";

const scope = { user_id: "u_original", team_id: "team_original", workspace_id: "ws_original", session_id: "s_original" };
const reference = { kind: "skill" as const, id: "sk_original", source_kind: "user" as const, revision: `sha256:${"a".repeat(64)}` };
const original: CoachEditorSnapshot = { target: reference, dirty: false, pending_write: false };
afterEach(() => _resetAllTurnsForTesting());

it("captures the editor baseline independently from later selection or background versions", () => {
  const state = structuredClone(original);
  const binding = captureCoachCanvasBinding(scope, { ...reference, revision: "a-new-background-revision" }, { requestClose: vi.fn(), capture: () => state });
  state.target.id = "sk_another";
  expect(binding.target).toEqual(reference);
  expect(parseCoachCanvasBinding(binding)).toEqual(binding);
  expect(() => captureCoachCanvasBinding(scope, reference, { requestClose: vi.fn(), capture: () => state })).toThrow("page_not_ready");
});

it.each([{ dirty: true }, { pending_write: true }])("retains input instead of submitting a busy or dirty editor: %j", (changes) => {
  expect(() => captureCoachCanvasBinding(scope, reference, { requestClose: vi.fn(), capture: () => ({ ...original, ...changes }) })).toThrow("local_changes");
});

it("rejects missing controllers and corrupt recovered targets", () => {
  expect(() => captureCoachCanvasBinding(scope, reference, null)).toThrow("page_not_ready");
  const binding = captureCoachCanvasBinding(scope, reference, { requestClose: vi.fn(), capture: () => original });
  expect(parseCoachCanvasBinding({ ...binding, extra: "hidden form content" })).toBeNull();
  expect(parseCoachCanvasBinding({ ...binding, target: { kind: "agent", id: "osa_1", version: 1, live_release_id: "arl_1", working_copy: { id: "awc_1", version: -1, base_release_id: null } } })).toBeNull();
});

it("projects the same safe original target through live, replay and later Turn generations", () => {
  const binding: CoachCanvasBinding = { schema_version: 1, ...scope, ...original };
  const event = { turn_id: "t_original", session_id: scope.session_id, workspace_id: scope.workspace_id,
    started_at: "2026-10-03T00:00:00Z", coach_canvas_target: binding };
  expect(dispatchToTurnStore("turn_started", event)).toBe(true);
  const live = getTurnSnapshot("t_original")?.coachCanvasTarget;
  _resetAllTurnsForTesting();
  dispatchToTurnStore("turn_started", event);
  expect(getTurnSnapshot("t_original")?.coachCanvasTarget).toEqual(live);
  dispatchToTurnStore("turn_started", { ...event, coach_canvas_target: { ...binding, target: { ...reference, id: "sk_later" } } });
  expect(getTurnSnapshot("t_original")?.coachCanvasTarget).toEqual(binding);
  dispatchToTurnStore("turn_started", { ...event, turn_id: "t_wrong", session_id: "s_another" });
  expect(getTurnSnapshot("t_wrong")?.coachCanvasTarget).toBeUndefined();
});

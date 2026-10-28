import metadataFixture from "@/__tests__/fixtures/configuration-space-metadata.json";
import { expect, it } from "vitest";
import { dispatchToTurnStore } from "./turn-store-dispatch";
import { configurationCanvasOpen, configurationCanvasProposalIds, configurationCanvasVersion } from "./configuration-canvas-events";
import { replayTurnEventRows } from "./turn-store-replay";
import { getLiteProposal } from "./config-copilot-lite";

it("replays a persisted Tool object and proposal into the same Canvas projection", () => {
  const pointer = { page: "workspace_i18n", user_id: "u_original", team_id: "team_original", workspace_id: "ws_original" };
  const payload = { turn_id: "t_replay_configuration", target: pointer };
  expect(dispatchToTurnStore("configuration.canvas_open", payload)).toBe(true);
  const opened = configurationCanvasOpen(payload.turn_id);
  expect(opened?.target.workspaceId).toBe("ws_original");
  const version = configurationCanvasVersion();
  dispatchToTurnStore("configuration.canvas_open", payload);
  expect(configurationCanvasOpen(payload.turn_id)).toBe(opened);
  expect(configurationCanvasVersion()).toBe(version);
  dispatchToTurnStore("config.lite_propose", { turn_id: payload.turn_id, proposal_id: "ccd_replayed", form_id: "workspace_i18n", rows: [] });
  expect(configurationCanvasProposalIds(payload.turn_id)).toEqual(["ccd_replayed"]);
  expect(configurationCanvasOpen("t_other")).toBeNull();
});

it("projects the admitted metadata fixture identically for different caller labels and replay", () => {
  for (const agent of ["configuration-agent", "lead"]) {
    const payload = { ...metadataFixture.open, agent };
    expect(dispatchToTurnStore("configuration.canvas_open", payload)).toBe(true);
    expect(configurationCanvasOpen(payload.turn_id)?.target).toEqual({ page: "space_general", userId: payload.target.user_id, teamId: payload.target.team_id, workspaceId: payload.target.workspace_id });
  }
  dispatchToTurnStore("configuration.canvas_open", metadataFixture.open);
  expect(configurationCanvasOpen(metadataFixture.open.turn_id)?.target.page).toBe("space_general");
  for (let replay = 0; replay < 2; replay++) dispatchToTurnStore("config.lite_propose", metadataFixture.review);
  expect(getLiteProposal(metadataFixture.review.proposal_id)?.rows).toEqual(metadataFixture.review.rows);
  expect(configurationCanvasProposalIds(metadataFixture.open.turn_id)).toEqual([metadataFixture.review.proposal_id]);
});


it("replay restores the same object pointer without becoming a live controller admission", () => {
  const target = { page: "workspace_i18n", user_id: "u_original", team_id: "team_original", workspace_id: "ws_original" };
  const payload = { turn_id: "t_saved_review", target };
  replayTurnEventRows(payload.turn_id, [{ seq: 1, occurred_at: "2026-10-03T00:00:00Z", event_type: "configuration.canvas_open", payload }]);
  const opened = configurationCanvasOpen(payload.turn_id);
  expect(opened).toMatchObject({ source: "replay", target: { workspaceId: "ws_original" } });
  dispatchToTurnStore("configuration.canvas_open", payload);
  expect(configurationCanvasOpen(payload.turn_id)).toBe(opened);
  expect(opened?.source).toBe("replay");
});

import { expect, it } from "vitest";
import { documentCanvasOpen, documentCanvasVersion, parseDocumentDraftOpen } from "./document-canvas-events";
import { dispatchToTurnStore } from "./turn-store-dispatch";
import { replayTurnEventRows } from "./turn-store-replay";

const pointer = { version: 1, turn_id: "t_document_event", session_id: "s_doc", user_id: "u_doc",
  team_id: "team_doc", workspace_id: "ws_doc", job_id: "djob_original", draft_digest: "a".repeat(64) };

it("uses the same immutable pointer for live, persisted replay and duplicate delivery", () => {
  dispatchToTurnStore("document.draft_open", pointer, "live", pointer.turn_id);
  const original = documentCanvasOpen(pointer.turn_id);
  const revision = documentCanvasVersion();
  expect(original).toEqual(pointer);
  replayTurnEventRows(pointer.turn_id, [{ seq: 1, occurred_at: "2026-10-04", event_type: "document.draft_open", payload: pointer }]);
  dispatchToTurnStore("document.draft_open", { ...pointer, job_id: "djob_replacement" }, "live", pointer.turn_id);
  expect(documentCanvasOpen(pointer.turn_id)).toBe(original);
  expect(documentCanvasVersion()).toBe(revision);
});

it("requires the actual transport Turn and rejects content, unknown versions and malformed pointers", () => {
  const turn_id = "t_document_invalid";
  for (const changed of [{ version: 2 }, { version: true }, { version: undefined }, { job_id: "https://example.com" },
    { draft_digest: "unknown" }, { workspace_id: "" }, { user_id: " u_doc" }, { title: "Model text" }, { card_id: "t_foreign" }]) {
    expect(parseDocumentDraftOpen({ ...pointer, turn_id, ...changed }, turn_id)).toBeNull();
  }
  dispatchToTurnStore("document.draft_open", { ...pointer, turn_id }, "live", "t_foreign");
  dispatchToTurnStore("document.draft_open", { ...pointer, turn_id });
  replayTurnEventRows("t_foreign", [{ seq: 2, occurred_at: "2026-10-04", event_type: "document.draft_open", payload: { ...pointer, turn_id } }]);
  expect(documentCanvasOpen(turn_id)).toBeNull();
});

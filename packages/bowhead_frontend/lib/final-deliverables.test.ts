// @vitest-environment node
import { afterEach, describe, expect, it } from "vitest";
import { extractInlineArtifacts } from "./inline-artifacts";
import { dispatchToTurnStore } from "./turn-store-dispatch";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";

afterEach(_resetAllTurnsForTesting);

const turnId = "t_html_revision";
const file = (id: string, filename = "fibonacci.html", mime = "text/html") => ({
  kind: "file", id, filename, mime, download_url: `/api/v1/files/${id}`,
});

function revision(inputIds: string[], { historical = false, state = "success", id = "art_final" } = {}) {
  dispatchToTurnStore("step_started", {
    card_id: turnId, step_id: id, tool_name: "python_sandbox", step_type: "tool_run",
    header_zh: "Run code", subagent_id: "sub_compute", started_at: "2026-11-06T12:00:00Z",
    code_input: { language: "python", code: "print('revision')", truncated: false, ...(!historical ? { input_artifact_ids: inputIds } : {}) },
    arguments_preview: JSON.stringify({ inputs: Object.fromEntries(inputIds.map((artifact_id, i) => [i, { artifact_id }])) }),
  });
  dispatchToTurnStore("step_done", {
    card_id: turnId, step_id: id, state,
    result_meta: { output_artifacts: [{ artifact_id: id, filename: "fibonacci.html", mime: "text/html" }] },
  });
}

function displayed(files = [file("art_draft"), file("art_final"), file("art_other")]) {
  return extractInlineArtifacts({ inline_artifacts: files }, getTurnSnapshot(turnId)?.steps).map((item) => item.id);
}

describe("final file delivery from live and replayed steps", () => {
  it.each([false, true])("keeps the corrected HTML and independent same-name files (historical=%s)", (historical) => {
    revision(["art_draft"], { historical });
    expect(displayed()).toEqual(["art_final", "art_other"]);
    expect(getTurnSnapshot(turnId)?.steps[0].outputArtifacts?.[0].artifactId).toBe("art_final");
  });

  it("keeps intermediate revisions in the execution process", () => {
    revision([], { id: "art_draft" });
    revision(["art_draft"], { id: "art_middle" });
    revision(["art_middle"]);
    expect(displayed([file("art_draft"), file("art_middle"), file("art_final")])).toEqual(["art_final"]);
    expect(getTurnSnapshot(turnId)?.steps.flatMap((step) => step.outputArtifacts?.map((item) => item.artifactId) ?? []))
      .toEqual(["art_draft", "art_middle", "art_final"]);
  });

  it.each(["failed", "partial", "cancelled"])("does not replace a file after a %s revision", (state) => {
    revision(["art_draft"], { state });
    expect(displayed()).toEqual(["art_draft", "art_final", "art_other"]);
  });

  it.each([{ ids: [] }, { ids: ["art_draft", "art_other"] }, { ids: ["art_unknown"] }])("preserves ambiguous or absent input proof: %j", ({ ids }) => {
    revision(ids);
    expect(displayed()).toEqual(["art_draft", "art_final", "art_other"]);
  });

  it("keeps a source when the output has another filename or MIME", () => {
    revision(["art_draft"]);
    expect(displayed([file("art_draft", "source.html"), file("art_final")])).toEqual(["art_draft", "art_final"]);
    expect(displayed([file("art_draft", "fibonacci.html", "text/plain"), file("art_final")])).toEqual(["art_draft", "art_final"]);
  });
});

function rewrite(id: string, helper: string, { state = "success", filename = "game.html", path = "" } = {}) {
  dispatchToTurnStore("step_started", {
    card_id: turnId, step_id: `step_${id}`, tool_name: "python_sandbox", step_type: "tool_run",
    header_zh: "Run code", subagent_id: helper, started_at: "2026-11-17T03:30:00Z",
    code_input: { language: "python", code: "write()", truncated: false, input_artifact_ids: [] },
  });
  dispatchToTurnStore("step_done", {
    card_id: turnId, step_id: `step_${id}`, state,
    result_meta: { output_artifacts: [{ artifact_id: id, filename, mime: "text/html", ...(path ? { path } : {}) }] },
  });
}

describe("same-name files a helper rewrote in one Turn", () => {
  const game = (id: string) => file(id, "game.html");

  it("shows only the newest copy and keeps every copy in the execution process", () => {
    // Observed case: one helper wrote the game six times and the answer listed six cards.
    rewrite("art_1", "sub_compute");
    rewrite("art_2", "sub_compute", { state: "failure" });
    rewrite("art_3", "sub_compute");
    expect(displayed([game("art_3"), game("art_2"), game("art_1")])).toEqual(["art_3"]);
    expect(getTurnSnapshot(turnId)?.steps.flatMap((step) => step.outputArtifacts?.map((item) => item.artifactId) ?? []))
      .toEqual(["art_1", "art_2", "art_3"]);
  });

  it("keeps same-name files of different helpers and files no step reports", () => {
    rewrite("art_east", "sub_east");
    rewrite("art_west", "sub_west");
    expect(displayed([game("art_west"), game("art_east"), game("art_upload")])).toEqual(["art_west", "art_east", "art_upload"]);
  });

  it("keeps same-name files of different declared folders and hides a rewrite of one path", () => {
    const page = (id: string) => file(id, "index.html");
    rewrite("art_home", "sub_compute", { filename: "index.html", path: "site/index.html" });
    rewrite("art_blog", "sub_compute", { filename: "index.html", path: "site/blog/index.html" });
    rewrite("art_home_2", "sub_compute", { filename: "index.html", path: "site/index.html" });
    expect(displayed([page("art_home_2"), page("art_blog"), page("art_home")])).toEqual(["art_home_2", "art_blog"]);
  });

  it("keeps a copy whose reported name differs from the delivered file", () => {
    rewrite("art_1", "sub_compute");
    rewrite("art_2", "sub_compute", { filename: "other.html" });
    expect(displayed([game("art_2"), game("art_1")])).toEqual(["art_2", "art_1"]);
  });
});


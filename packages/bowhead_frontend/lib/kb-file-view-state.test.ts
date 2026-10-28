import { expect, it } from "vitest";
import { readKbFileView, writeKbFileView } from "./kb-file-view-state";

it("restores every supported view control while preserving an open source and navigation", () => {
  const query = "tab=files&scope=space&space=ws_a&q=合同&deep=1&status=done,failed&type=pdf,sheet&days=30&sort=name&order=asc&page=3&file_id=kbf_a&chunk_id=ch_a";
  const view = readKbFileView(query);
  expect(view).toEqual({ scope: "space", query: "合同", deep: true, status: ["done", "failed"], types: ["pdf", "sheet"], days: 30, sort: "name", order: "asc", page: 3 });
  const updated = writeKbFileView(query, { ...view, scope: "space", query: "新合同", page: 1 });
  expect(updated.get("file_id")).toBe("kbf_a");
  expect(updated.get("chunk_id")).toBe("ch_a");
  expect(updated.get("space")).toBe("ws_a");
  expect(readKbFileView(updated.toString())).toEqual({ ...view, query: "新合同", page: 1 });
});

it("uses safe defaults for malformed or unsupported filters", () => {
  expect(readKbFileView("scope=workspace&page=-2&status=done,secret,done&type=pdf,exe&days=1&sort=sql&order=drop")).toMatchObject({
    scope: "space", page: 1, status: ["done"], types: ["pdf"], days: null, sort: "uploadedAt", order: "desc",
  });
});

it("keeps the all-scopes filter in the URL and reports a missing or unknown scope as null", () => {
  expect(readKbFileView("scope=all").scope).toBe("all");
  expect(readKbFileView("scope=personal").scope).toBe("personal");
  expect(writeKbFileView("tab=files", { ...readKbFileView(""), scope: "all" }).get("scope")).toBe("all");
  // The caller decides: it keeps the remembered or current scope (sidebar link).
  expect(readKbFileView("tab=files").scope).toBeNull();
  expect(readKbFileView("scope=everything").scope).toBeNull();
});

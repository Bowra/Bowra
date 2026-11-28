// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api-client";
import {
  type KnowledgeBaseDirectory,
  type KnowledgeBaseDTO,
  createKnowledgeBase,
  deleteKnowledgeBase,
  directoryKnowledgeBases,
  knowledgeBaseOfFile,
  listKnowledgeBaseFiles,
  moveKnowledgeBaseFiles,
  putKnowledgeBaseMembers,
  updateKnowledgeBase,
} from "./knowledge-bases-api";

function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function call(fetchMock: ReturnType<typeof stubFetch>, index = 0): { url: URL; init: RequestInit } {
  const [input, init] = fetchMock.mock.calls[index];
  return { url: new URL(String(input), "http://app.test"), init: init ?? {} };
}

function kb(partial: Partial<KnowledgeBaseDTO>): KnowledgeBaseDTO {
  return {
    id: "kbase_x",
    is_default: false,
    home: "space",
    home_id: "ws_1",
    name: "Manuals",
    description: null,
    audience: "space",
    audience_level: "use",
    level: "use",
    owner: null,
    can: { add_files: false, manage: false, transfer: false, delete: false },
    file_count: 0,
    failed_count: 0,
    updated_at: null,
    revision: 1,
    ...partial,
  };
}

describe("knowledge base API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("creates with a JSON body", async () => {
    const fetchMock = stubFetch(kb({}), 201);
    await createKnowledgeBase({ name: "Manuals", home: "space", audience: "space", audience_level: "edit" });
    const { url, init } = call(fetchMock);
    expect(url.pathname).toBe("/api/v1/kb/knowledge-bases");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ name: "Manuals", home: "space", audience: "space", audience_level: "edit" });
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("patches with the revision and deletes with the confirmed file count", async () => {
    const fetchMock = stubFetch(kb({}));
    await updateKnowledgeBase("kbase_x", { revision: 3, name: "New" });
    expect(call(fetchMock).url.pathname).toBe("/api/v1/kb/knowledge-bases/kbase_x");
    expect(JSON.parse(String(call(fetchMock).init.body))).toEqual({ revision: 3, name: "New" });

    await deleteKnowledgeBase("kbase_x", { file_count: 7, revision: 3 });
    const deleted = call(fetchMock, 1);
    expect(deleted.init.method).toBe("DELETE");
    expect(Object.fromEntries(deleted.url.searchParams)).toEqual({ file_count: "7", revision: "3" });
  });

  it("sends member changes, a null level removing an entry", async () => {
    const fetchMock = stubFetch({ entries: [] });
    await putKnowledgeBaseMembers("kbase_x", [{ principal_type: "user", principal_id: "u_2", level: null }]);
    const { url, init } = call(fetchMock);
    expect(url.pathname).toBe("/api/v1/kb/knowledge-bases/kbase_x/members");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ changes: [{ principal_type: "user", principal_id: "u_2", level: null }] });
  });

  it("lists one knowledge base's files with today's filters and no scope", async () => {
    const fetchMock = stubFetch({ files: [], total: 0 });
    await listKnowledgeBaseFiles("kbase_x", { offset: 20, limit: 20, status: "failed", q: "plan", order_by: "filename", order_dir: "asc" });
    const { url } = call(fetchMock);
    expect(url.pathname).toBe("/api/v1/kb/knowledge-bases/kbase_x/files");
    expect(Object.fromEntries(url.searchParams)).toEqual({ offset: "20", limit: "20", status: "failed", q: "plan", order_by: "filename", order_dir: "asc" });
  });

  it("previews a move with dry_run", async () => {
    const fetchMock = stubFetch({ dry_run: true, results: [], succeeded: 1, failed: 0, unchanged: 0, movable: 1, gains: 2, losses: 1 });
    const result = await moveKnowledgeBaseFiles({ file_ids: ["kbf_1"], target_knowledge_base_id: "kbase_y", dry_run: true });
    const { url, init } = call(fetchMock);
    expect(url.pathname).toBe("/api/v1/kb/knowledge-bases/files/move");
    expect(JSON.parse(String(init.body))).toEqual({ file_ids: ["kbf_1"], target_knowledge_base_id: "kbase_y", dry_run: true });
    expect(result.gains).toBe(2);
  });

  it("throws the API's error code, so the uniform not-found copy applies", async () => {
    stubFetch({ error: { code: "knowledge_base_not_found", message: "not found" } }, 404);
    const error = await updateKnowledgeBase("kbase_gone", { revision: 1 }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
    expect((error as ApiError).code).toBe("knowledge_base_not_found");
  });
});

describe("knowledgeBaseOfFile", () => {
  const directory: KnowledgeBaseDirectory = {
    space_id: "ws_1",
    sections: {
      personal: [kb({ id: "kbase_me", is_default: true, home: "personal", home_id: "u_1", name: null, audience: "owner" })],
      space: [
        kb({ id: "kbase_space", is_default: true, home: "space", home_id: "ws_1", name: null }),
        kb({ id: "kbase_manuals", home: "space", home_id: "ws_1" }),
      ],
      team: [kb({ id: "kbase_team", is_default: true, home: "team", home_id: "t_1", name: null, audience: "team" })],
    },
    can_create: { personal: true, space: true, team: false },
  };

  it("lists sections in My, Space, Team order", () => {
    expect(directoryKnowledgeBases(directory).map((item) => item.id)).toEqual(["kbase_me", "kbase_space", "kbase_manuals", "kbase_team"]);
    expect(directoryKnowledgeBases(null)).toEqual([]);
  });

  it("finds a named knowledge base by the file's ID", () => {
    expect(knowledgeBaseOfFile({ scope: "space", scope_id: "ws_1", knowledge_base_id: "kbase_manuals" }, directory)?.id).toBe("kbase_manuals");
  });

  it("falls back to the scope's default when the file names none (read safety rule)", () => {
    expect(knowledgeBaseOfFile({ scope: "space", scope_id: "ws_1" }, directory)?.id).toBe("kbase_space");
    expect(knowledgeBaseOfFile({ scope: "personal", scope_id: "u_1", knowledge_base_id: null }, directory)?.id).toBe("kbase_me");
    expect(knowledgeBaseOfFile({ scope: "team", scope_id: "t_1" }, directory)?.id).toBe("kbase_team");
  });

  it("falls back to the default when the named one is not listed, and finds nothing for another Space", () => {
    expect(knowledgeBaseOfFile({ scope: "space", scope_id: "ws_1", knowledge_base_id: "kbase_hidden" }, directory)?.id).toBe("kbase_space");
    expect(knowledgeBaseOfFile({ scope: "space", scope_id: "ws_2" }, directory)).toBeNull();
  });
});

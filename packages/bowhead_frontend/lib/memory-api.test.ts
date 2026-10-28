/**
 * V3 PRD-B · memory-api.ts unit tests.
 *
 * Mocks global fetch; verifies URL/method/body shape and error wrapping.
 */

import { headersExactly } from "@/__tests__/request-headers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  approveCandidate,
  beginAboutYouEditSession,
  deleteEntry,
  exportMemoryUrl,
  fetchCanonicalMemoryItems,
  fetchDreamingState,
  fetchMemoryOverview,
  fetchSnapshot,
  fetchTrash,
  getTurnMemoryReceipt,
  listCandidates,
  MemoryAPIError,
  patchAboutYou,
  patchCanonicalMemoryItem,
  pauseDreaming,
  rejectCandidate,
  releaseAboutYouEditSession,
  restoreEntry,
  restoreMemoryReset,
  resetMemory,
  resumeDreaming,
} from "./memory-api";

function _ok(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

function _err(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  // Fresh fetch spy each test
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
});

const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

// ─── V2 canonical read model ────────────────────────────

describe("V2 canonical read model", () => {
  it("fetches the overview without caching", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      schema_version: 2,
      about_you: {},
      suggestions: [],
      stats: {},
      learning: { automatic_learning: "active" },
      capabilities: {},
    }));

    const overview = await fetchMemoryOverview();

    expect(overview.learning.automatic_learning).toBe("active");
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/overview",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("serializes canonical item filters and cursor", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ items: [], next_cursor: null }));

    await fetchCanonicalMemoryItems({
      state: "trashed",
      query: "quarterly",
      displayType: "work_rule",
      scopeType: "space",
      spaceId: "ws_retail",
      limit: 25,
      cursor: "opaque-cursor",
    });

    const url = fetchMock().mock.calls[0][0] as string;
    expect(url).toContain("/api/v1/memory/items?");
    expect(url).toContain("state=trashed");
    expect(url).toContain("query=quarterly");
    expect(url).toContain("display_type=work_rule");
    expect(url).toContain("scope_type=space");
    expect(url).toContain("space_id=ws_retail");
    expect(url).toContain("limit=25");
    expect(url).toContain("cursor=opaque-cursor");
  });

  it("sends the localized source-line keys with a search, even when none match", async () => {
    fetchMock().mockImplementation(async () => _ok({ items: [], next_cursor: null }));

    await fetchCanonicalMemoryItems({ query: "conversation", sourceLabelKeys: ["told_in_chat", "auto_captured"] });
    await fetchCanonicalMemoryItems({ query: "zzqx", sourceLabelKeys: [] });
    await fetchCanonicalMemoryItems({ sourceLabelKeys: [] });

    const [withKeys, withoutMatches, noQuery] = fetchMock().mock.calls.map((call) => new URL(call[0] as string, "http://x"));
    expect(withKeys.searchParams.get("source_label_keys")).toBe("told_in_chat,auto_captured");
    expect(withoutMatches.searchParams.get("source_label_keys")).toBe("");
    expect(noQuery.searchParams.has("source_label_keys")).toBe(false);
  });

  it("PATCHes a canonical item with typed payload and version", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      id: "mem_1",
      display_value: "bar",
      version: "2",
    }));

    await patchCanonicalMemoryItem("mem_1", {
      schema: "text_v1",
      patch: { content: "bar" },
      version: "1",
    });

    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/items/mem_1",
      expect.objectContaining({
        method: "PATCH",
        cache: "no-store",
        body: JSON.stringify({
          schema: "text_v1",
          patch: { content: "bar" },
          version: "1",
        }),
      }),
    );
  });

  it("acquires, saves, and releases an About-you owner lease", async () => {
    fetchMock()
      .mockResolvedValueOnce(_ok({
        edit_token: "edit-token",
        version: "4",
        expires_at: "2026-07-27T08:05:00Z",
      }))
      .mockResolvedValueOnce(_ok({ content: "updated", version: "5" }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await beginAboutYouEditSession("4");
    await patchAboutYou({
      content: "updated",
      version: "4",
      editToken: "edit-token",
    });
    await releaseAboutYouEditSession("edit-token");

    expect(fetchMock()).toHaveBeenNthCalledWith(
      1,
      "/api/v1/memory/about-you/edit-session",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ expected_version: "4" }),
      }),
    );
    expect(fetchMock()).toHaveBeenNthCalledWith(
      2,
      "/api/v1/memory/about-you",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          content: "updated",
          version: "4",
          edit_token: "edit-token",
        }),
      }),
    );
    expect(fetchMock()).toHaveBeenNthCalledWith(
      3,
      "/api/v1/memory/about-you/edit-session",
      expect.objectContaining({
        method: "DELETE",
        headers: headersExactly({ "X-Memory-Edit-Token": "edit-token" }),
      }),
    );
  });
});

// ─── snapshot ────────────────────────────────────────────

describe("fetchSnapshot", () => {
  it("GETs /api/v1/memory/snapshot and returns body", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      user_id: "u1", items: [], stats: {
        active_count: 0, trash_count: 0, pending_candidates: 0,
      }, dreaming: { paused: false },
    }));
    const snap = await fetchSnapshot();
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/snapshot",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(snap.user_id).toBe("u1");
    expect(snap.dreaming.paused).toBe(false);
  });

  it("throws MemoryAPIError with detail on 4xx", async () => {
    fetchMock().mockResolvedValueOnce(_err(404, "not found"));
    let caught: unknown = null;
    try {
      await fetchSnapshot();
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(MemoryAPIError);
    expect((caught as MemoryAPIError).status).toBe(404);
    expect((caught as MemoryAPIError).detail).toBe("not found");
  });
});

// ─── trash + delete + restore ───────────────────────────

describe("fetchTrash", () => {
  it("GETs /memory/trash", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ user_id: "u1", items: [] }));
    const r = await fetchTrash();
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/trash",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(r.user_id).toBe("u1");
  });
});

describe("deleteEntry", () => {
  it("DELETEs /memory/{id} (URL-encoded)", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ deleted: "mem_abc" }));
    const r = await deleteEntry("mem_abc");
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/mem_abc",
      expect.objectContaining({ method: "DELETE", credentials: "include" }),
    );
    expect(r.deleted).toBe("mem_abc");
  });

  it("encodes special chars in id", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ deleted: "x/y" }));
    await deleteEntry("x/y");
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/x%2Fy",
      expect.anything(),
    );
  });

  it("sends a quoted If-Match token for canonical callers", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ deleted: "mem_abc" }));
    await deleteEntry("mem_abc", "7");
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/mem_abc",
      expect.objectContaining({
        headers: headersExactly({ "If-Match": '"7"' }),
      }),
    );
  });
});

describe("restoreEntry", () => {
  it("POSTs /memory/{id}/restore", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ id: "mem_x", restored: true }));
    const r = await restoreEntry("mem_x");
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/mem_x/restore",
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
    expect(r.restored).toBe(true);
  });
});

describe("resetMemory", () => {
  it("generates a canonical UUID when randomUUID is unavailable", async () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.fill(0xab);
        return bytes;
      },
    });
    fetchMock().mockResolvedValueOnce(_ok({
    }));

    await resetMemory();

    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/reset",
      expect.objectContaining({
        headers: headersExactly({
          "Idempotency-Key": "abababab-abab-4bab-abab-abababababab",
        }),
      }),
    );
  });

  it("POSTs /memory/reset with the caller idempotency key", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
    }));
    const idempotencyKey = "781d8bd9-56b1-443d-a3bd-2bbbdd10a025";
    const r = await resetMemory(idempotencyKey);
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/reset",
      expect.objectContaining({
        method: "POST",
        headers: headersExactly({ "Idempotency-Key": idempotencyKey }),
      }),
    );
    expect(r.snapshot_id).toBe("snap_abc");
    expect(r.affected_memory).toBe(3);
  });

  it("reuses a caller idempotency key for reset restore", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      snapshot_id: "snap_abc",
      status: "restored",
      restored_counts: {},
      conflicts: [],
      unavailable_counts: {},
      remaining_recoverable: 0,
    }));
    const idempotencyKey = "f4da969c-7bd4-42ef-8720-1366b8f305ee";

    await restoreMemoryReset("snap_abc", idempotencyKey);

    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/resets/snap_abc/restore",
      expect.objectContaining({
        method: "POST",
        headers: headersExactly({ "Idempotency-Key": idempotencyKey }),
      }),
    );
  });
});

// ─── export ─────────────────────────────────────────────

describe("exportMemoryUrl", () => {
  it("returns the export endpoint URL string", () => {
    expect(exportMemoryUrl()).toBe("/api/v1/memory/export");
  });

  it("applies the current runtime path prefix", () => {
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: null,
      teamId: null,
      baseUrl: "https://public.example/ai/bowhead",
      basePath: "/ai/bowhead",
      matchSource: "trusted_proxy_bootstrap",
    };

    expect(exportMemoryUrl()).toBe("/ai/bowhead/api/v1/memory/export");
  });
});

// ─── dreaming pause toggle ──────────────────────────────

describe("dreaming state toggle", () => {
  it("fetchDreamingState GETs /memory/dreaming", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ paused: false }));
    expect((await fetchDreamingState()).paused).toBe(false);
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/dreaming",
      expect.anything(),
    );
  });

  it("pauseDreaming POSTs /memory/dreaming/pause", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ paused: true }));
    const r = await pauseDreaming();
    expect(r.paused).toBe(true);
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/dreaming/pause",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("resumeDreaming POSTs /memory/dreaming/resume", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ paused: false }));
    const r = await resumeDreaming();
    expect(r.paused).toBe(false);
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/dreaming/resume",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("sends a quoted If-Match token for canonical callers", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ id: "mem_x", restored: true }));
    await restoreEntry("mem_x", "8");
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/mem_x/restore",
      expect.objectContaining({
        headers: headersExactly({ "If-Match": '"8"' }),
      }),
    );
  });
});

// ─── candidates ─────────────────────────────────────────

describe("listCandidates", () => {
  it("default status=pending, limit=50", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ items: [], scope: "pending" }));
    await listCandidates();
    const url = fetchMock().mock.calls[0][0] as string;
    expect(url).toContain("/api/v1/memory/candidates?");
    expect(url).toContain("status=pending");
    expect(url).toContain("limit=50");
  });

  it("supports status=all", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ items: [], scope: "all" }));
    await listCandidates("all", 100);
    const url = fetchMock().mock.calls[0][0] as string;
    expect(url).toContain("status=all");
    expect(url).toContain("limit=100");
  });
});

describe("getTurnMemoryReceipt", () => {
  it("reads the per-turn receipt with an encoded turn id", async () => {
    const body = {
      schema_version: 1,
      turn_id: "t_1/x",
      explicit_request: null,
      pending_candidates: [],
      entries: [],
    };
    fetchMock().mockResolvedValueOnce(_ok(body));
    expect(await getTurnMemoryReceipt("t_1/x")).toEqual(body);
    expect(fetchMock().mock.calls[0][0]).toBe(
      "/api/v1/memory/turn-receipt?turn_id=t_1%2Fx",
    );
  });
});

describe("approveCandidate", () => {
  it("POSTs /candidates/{cid}/approve with reason in body", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      id: "cand_x", userId: "u1", status: "approved",
      kind: "preference", key: "k", value: "v",
      reason: null, sourceTurnId: null, decidedBy: "u1",
      decidedAt: "2026-05-02", createdAt: "x", updatedAt: "x",
    }));
    await approveCandidate("cand_x", "looks good");
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/candidates/cand_x/approve",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining("looks good"),
      }),
    );
  });

  it("sends reason=null when omitted", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      id: "cand_y", userId: "u1", status: "approved",
      kind: "preference", key: "k", value: "v",
      reason: null, sourceTurnId: null, decidedBy: "u1",
      decidedAt: "x", createdAt: "x", updatedAt: "x",
    }));
    await approveCandidate("cand_y");
    const body = JSON.parse(
      (fetchMock().mock.calls[0][1] as RequestInit).body as string,
    );
    expect(body.reason).toBeNull();
  });

  it("submits the server target identity without inferring a store", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      id: "cand_space", userId: "u1", status: "approved",
      kind: "note", key: null, value: "v",
      reason: null, sourceTurnId: null, decidedBy: "u1",
      decidedAt: "x", createdAt: "x", updatedAt: "x",
    }));
    await approveCandidate(
      "cand_space",
      undefined,
      {
        type: "space",
        space_id: "ws_retail",
        space_name: "Retail",
        availability: "available",
        label: "Only you · Retail",
        requires_confirmation: false,
      },
    );
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/candidates/cand_space/approve",
      expect.objectContaining({
        body: JSON.stringify({
          reason: null,
          target: {
            type: "space",
            space_id: "ws_retail",
          },
        }),
      }),
    );
  });
});

describe("rejectCandidate", () => {
  it("POSTs /candidates/{cid}/reject", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      id: "cand_z", userId: "u1", status: "rejected",
      kind: "preference", key: "k", value: "v",
      reason: null, sourceTurnId: null, decidedBy: "u1",
      decidedAt: "x", createdAt: "x", updatedAt: "x",
    }));
    await rejectCandidate("cand_z", "not relevant");
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/candidates/cand_z/reject",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

// ─── bulk-decide ────────────────────────────────────────

import { bulkDecideCandidates } from "./memory-api";

describe("bulkDecideCandidates", () => {
  it("POSTs cids array + action, returns result", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      action: "approve",
      total: 2, succeeded: 2, failed: 0,
      items: [
        { cid: "a", ok: true, status: "approved", error: null },
        { cid: "b", ok: true, status: "approved", error: null },
      ],
    }));
    const r = await bulkDecideCandidates(["a", "b"], "approve");
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/candidates/bulk-decide",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining("approve"),
      }),
    );
    expect(r.succeeded).toBe(2);
    expect(r.items).toHaveLength(2);
  });

  it("propagates server 400 as MemoryAPIError", async () => {
    fetchMock().mockResolvedValueOnce(_err(400, "max 200"));
    let caught: unknown = null;
    try {
      await bulkDecideCandidates(["x"], "approve");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(MemoryAPIError);
    expect((caught as MemoryAPIError).status).toBe(400);
  });
});

// ─── V3.5 PRD-C notes wrappers ─────────────────────────

import {
  appendBullet,
  deleteBullet,
  fetchNotes,
  lockNotes,
  patchNotes,
  unlockNotes,
} from "./memory-api";

describe("memory_notes wrappers", () => {
  it("fetchNotes GETs /memory/notes", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      user_id: "u1", content: "", char_count: 0, pinned_count: 0,
      bullet_count: 0, free_text_lines: 0,
      last_edited_at: null, last_edited_source: "",
      is_locked: false, lock_expires_at: null,
    }));
    await fetchNotes();
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/notes",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("patchNotes PATCHes content with owner token and version", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      user_id: "u1", content: "hi", char_count: 2,
      pinned_count: 0, bullet_count: 0, free_text_lines: 1,
      last_edited_at: "x", last_edited_source: "user_explicit",
      is_locked: false, lock_expires_at: null,
    }));
    await patchNotes("hi", {
      version: "2",
      editToken: "edit-token",
    });
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/notes",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          content: "hi",
          version: "2",
          edit_token: "edit-token",
          ignore_lock: false,
        }),
      }),
    );
  });

  it("appendBullet POSTs /append with pinned + ignore_lock", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      user_id: "u1", content: "📌 hi", char_count: 4,
      pinned_count: 1, bullet_count: 0, free_text_lines: 0,
      last_edited_at: "x", last_edited_source: "user_explicit",
      is_locked: false, lock_expires_at: null,
    }));
    await appendBullet({ bullet: "hi", pinned: true });
    const req = fetchMock().mock.calls[0][1] as RequestInit;
    const body = JSON.parse(req.body as string);
    expect(body.bullet).toBe("hi");
    expect(body.pinned).toBe(true);
  });

  it("deleteBullet DELETEs with query string", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      user_id: "u1", content: "", char_count: 0,
      pinned_count: 0, bullet_count: 0, free_text_lines: 0,
      last_edited_at: null, last_edited_source: "",
      is_locked: false, lock_expires_at: null,
    }));
    await deleteBullet(2, {
      version: "2",
      editToken: "edit-token",
    });
    const url = fetchMock().mock.calls[0][0] as string;
    const init = fetchMock().mock.calls[0][1] as RequestInit;
    expect(url).toContain("bullet_index=2");
    expect(url).toContain("/api/v1/memory/notes/bullet?");
    expect(init.headers).toEqual(headersExactly({
      "If-Match": '"2"',
      "X-Memory-Edit-Token": "edit-token",
    }));
  });

  it("lockNotes returns NotesLockResult", async () => {
    fetchMock().mockResolvedValueOnce(_ok({
      locked_until: "2026-05-03T10:00:00",
      edit_token: "edit-token",
      version: "2",
    }));
    const r = await lockNotes("2");
    expect(r.locked_until).toBe("2026-05-03T10:00:00");
    const init = fetchMock().mock.calls[0][1] as RequestInit;
    expect(init.body).toBe(JSON.stringify({ expected_version: "2" }));
  });

  it("unlockNotes POSTs /unlock", async () => {
    fetchMock().mockResolvedValueOnce(_ok({ ok: true }));
    await unlockNotes("edit-token");
    expect(fetchMock()).toHaveBeenCalledWith(
      "/api/v1/memory/notes/unlock",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ edit_token: "edit-token" }),
      }),
    );
  });

  it("patchNotes 413 raises MemoryAPIError", async () => {
    fetchMock().mockResolvedValueOnce(_err(413, "content > 3000 char"));
    let caught: unknown = null;
    try {
      await patchNotes("x".repeat(4000), {
        version: "2",
        editToken: "edit-token",
      });
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(MemoryAPIError);
    expect((caught as MemoryAPIError).status).toBe(413);
  });
});

// ─── error class ────────────────────────────────────────

describe("MemoryAPIError", () => {
  it("falls back to status string when detail unparseable", async () => {
    fetchMock().mockResolvedValueOnce(new Response("not json", { status: 500 }));
    await expect(fetchSnapshot()).rejects.toMatchObject({ status: 500 });
  });
});

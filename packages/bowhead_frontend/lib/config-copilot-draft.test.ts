/** Configuration-draft library contracts (tech-design-batch1b §5 tests 11, 15).
 *
 * Dispatcher insertion (per-turn idempotency / one card per turn across turns / discard bad payloads), hydrate (structured 404
 * → stale, bare 404/network errors retryable), consume/discard (409 repairs from current state), dismissed prevents reopening.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api-client", () => {
  class ApiError extends Error {
    status: number;
    body?: unknown;
    constructor(status: number, message: string, options: { body?: unknown } = {}) {
      super(message);
      this.status = status;
      this.body = options.body;
    }
  }
  return { apiJson: vi.fn(), ApiError };
});

import { ApiError, apiJson } from "@/lib/api-client";

import {
  _dropDraftStateCacheForTests,
  consumeDraft,
  discardDraft,
  getDraftPayload,
  getDraftResult,
  getDraftState,
  hydrateDraftState,
  markDraftDismissed,
  requestAutoOpenDraft,
  requestOpenDraft,
  resetDraftForTests,
  subscribeOpenDraft,
} from "./config-copilot-draft";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";
import { dispatchToTurnStore } from "./turn-store-dispatch";

const apiMock = vi.mocked(apiJson);

function dto(over: Record<string, unknown> = {}) {
  return {
    draft_id: "ccd_d1",
    form_id: "scheduled_job",
    status: "draft",
    workspace_id: "ws_1",
    fields: { name: "周报" },
    field_meta: { name: { origin: "generated", needs_confirm: false, note: null } },
    notes: {},
    result: null,
    row_version: 1,
    ...over,
  };
}

const wire = (over: Record<string, unknown> = {}) => ({
  seq: 1,
  card_id: "t_1",
  draft_id: "ccd_d1",
  turn_id: "t_1",
  form_id: "scheduled_job",
  created: true,
  updated_fields: ["name"],
  fields: { name: "周报" },
  field_meta: { name: { origin: "generated", needs_confirm: false, note: null } },
  notes: {},
  workspace_id: "ws_1",
  ...over,
});

beforeEach(() => {
  resetDraftForTests();
  _resetAllTurnsForTesting();
  apiMock.mockReset();
});

afterEach(() => {
  resetDraftForTests();
  _resetAllTurnsForTesting();
});

describe("dispatcher → draft beat", () => {
  it("keeps a collection draft target and cumulative fields in the replay dispatcher", () => {
    expect(dispatchToTurnStore("config.draft_updated", wire({
      form_id: "team_subagent", target: { id: "osa_1", label: "销售助手" },
      changed_fields_cumulative: ["description", "system_prompt"],
    }))).toBe(true);
    expect(getDraftPayload("ccd_d1")?.target).toEqual({ id: "osa_1", label: "销售助手" });
    expect(getDraftPayload("ccd_d1")?.changed_fields_cumulative).toEqual(["description", "system_prompt"]);
  });
  it("合法 payload → beat + payload store；同 turn 同 draftId 幂等；跨 turn 各留一张", () => {
    expect(dispatchToTurnStore("config.draft_updated", wire())).toBe(true);
    expect(dispatchToTurnStore("config.draft_updated", wire({ seq: 2, updated_fields: ["cron"] }))).toBe(true);
    const beats = getTurnSnapshot("t_1")!.beats.filter((b) => b.kind === "draft");
    expect(beats).toHaveLength(1); // per-turn idempotency
    // Second edit of the same draft falls in a new turn → retain one chip in that turn.
    expect(dispatchToTurnStore("config.draft_updated", wire({ seq: 3, turn_id: "t_2", card_id: "t_2" }))).toBe(true);
    expect(getTurnSnapshot("t_2")!.beats.filter((b) => b.kind === "draft")).toHaveLength(1);
    expect(getDraftPayload("ccd_d1")?.fields).toEqual({ name: "周报" });
  });

  it("坏 payload（缺 draft_id / fields / turn 锚点）→ 丢弃不崩（claimed=true，零 beat）", () => {
    expect(dispatchToTurnStore("config.draft_updated", wire({ draft_id: undefined }))).toBe(true);
    expect(dispatchToTurnStore("config.draft_updated", wire({ fields: undefined }))).toBe(true);
    expect(dispatchToTurnStore("config.draft_updated", wire({ turn_id: undefined, card_id: undefined }))).toBe(true);
    expect(getTurnSnapshot("t_1")?.beats.filter((b) => b.kind === "draft") ?? []).toHaveLength(0);
  });
});

describe("hydrateDraftState", () => {
  it("GET → 状态 + 回执；in-flight 去重；cache 命中零请求", async () => {
    let resolve: (v: unknown) => void = () => {};
    apiMock.mockReturnValueOnce(new Promise((r) => (resolve = r)) as Promise<unknown>);
    const a = hydrateDraftState("ccd_d1");
    const b = hydrateDraftState("ccd_d1");
    expect(apiMock).toHaveBeenCalledTimes(1);
    resolve(dto({ status: "consumed", result: { job_id: "sj_1", name: "周报" } }));
    await Promise.all([a, b]);
    expect(getDraftState("ccd_d1")).toBe("consumed");
    expect(getDraftResult("ccd_d1")).toEqual({ job_id: "sj_1", name: "周报" });
    await hydrateDraftState("ccd_d1");
    expect(apiMock).toHaveBeenCalledTimes(1);
  });

  it("结构化 404 → stale；裸 404（flag off）/网络错 → 不记可重试", async () => {
    apiMock.mockRejectedValueOnce(
      new ApiError(404, "nf", { body: { detail: { error: { code: "config_draft_not_found" } } } }),
    );
    await hydrateDraftState("ccd_gone");
    expect(getDraftState("ccd_gone")).toBe("stale");

    apiMock.mockRejectedValueOnce(new ApiError(404, "Not Found", { body: { detail: "Not Found" } }));
    await hydrateDraftState("ccd_flagoff");
    expect(getDraftState("ccd_flagoff")).toBeNull();

    apiMock.mockRejectedValueOnce(new TypeError("net"));
    await hydrateDraftState("ccd_net");
    expect(getDraftState("ccd_net")).toBeNull();
  });

  it("F5 语义：清 cache 后重新 hydrate 恢复服务端终态", async () => {
    apiMock.mockResolvedValueOnce(dto({ status: "discarded" }));
    await hydrateDraftState("ccd_d1");
    expect(getDraftState("ccd_d1")).toBe("discarded");
    _dropDraftStateCacheForTests();
    expect(getDraftState("ccd_d1")).toBeNull();
    apiMock.mockResolvedValueOnce(dto({ status: "discarded" }));
    await hydrateDraftState("ccd_d1");
    expect(getDraftState("ccd_d1")).toBe("discarded");
  });
});

describe("consume / discard", () => {
  it("consume → POST + 回执落 cache", async () => {
    apiMock.mockResolvedValueOnce(dto({ status: "consumed", result: { job_id: "sj_9" } }));
    await consumeDraft("ccd_d1", { job_id: "sj_9" });
    expect(apiMock.mock.calls[0][0]).toBe("/api/v1/config-drafts/ccd_d1/consume");
    expect(getDraftState("ccd_d1")).toBe("consumed");
    expect(getDraftResult("ccd_d1")).toEqual({ job_id: "sj_9" });
  });

  it("409 draft_state_conflict → 按服务端现态自愈不抛", async () => {
    apiMock.mockRejectedValueOnce(
      new ApiError(409, "c", { body: { detail: { error: { code: "draft_state_conflict", details: { status: "discarded" } } } } }),
    );
    await consumeDraft("ccd_d1", { job_id: "x" });
    expect(getDraftState("ccd_d1")).toBe("discarded");
  });

  it("discard → POST → discarded；失败静默", async () => {
    apiMock.mockResolvedValueOnce(dto({ status: "discarded" }));
    discardDraft("ccd_d1");
    await vi.waitFor(() => expect(getDraftState("ccd_d1")).toBe("discarded"));
    apiMock.mockRejectedValueOnce(new TypeError("net"));
    discardDraft("ccd_d2");
    await new Promise((r) => setTimeout(r, 0));
    expect(getDraftState("ccd_d2")).toBeNull();
  });
});

describe("打开通道", () => {
  it("chip 点击无视 dismissed；live 自动弹尊重 dismissed（SSE 重连不复弹）", () => {
    const seen: string[] = [];
    const off = subscribeOpenDraft((id) => seen.push(id));
    requestAutoOpenDraft("ccd_d1");
    expect(seen).toEqual(["ccd_d1"]);
    markDraftDismissed("ccd_d1");
    requestAutoOpenDraft("ccd_d1"); // Reconnection replay → no reopening
    expect(seen).toEqual(["ccd_d1"]);
    requestOpenDraft("ccd_d1"); // Explicit user click → open normally
    expect(seen).toEqual(["ccd_d1", "ccd_d1"]);
    off();
  });
});

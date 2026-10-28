/** Configuration Copilot lite library contract (tech-design-batch1-draft-store.md §9 tests 15–18 + batch 0.5 dispatcher test 24).
 *
 * Batch 1-a: server is the terminal-state source of truth—dispatcher adds to the stack (idempotency/bad payload/turn anchor unchanged),
 * hydrate (404→stale / network failure→retryable / in-flight deduplication), forward apply/undo POSTs
 * (zero settings API calls), self-repair to current state on 409 state_conflict, rehydrate after F5 (zero localStorage).
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

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
vi.mock("@/lib/use-theme-config", () => ({ refreshEffectiveTheme: vi.fn(async () => true) }));

import { ApiError, apiJson } from "@/lib/api-client";
import { refreshEffectiveTheme } from "@/lib/use-theme-config";
import { PERSONAL_APPEARANCE_REFRESH_EVENT } from "@/lib/personal-appearance";
import { WORKSPACE_SETTINGS_SAVED_EVENT } from "@/lib/workspace-settings-notifications";

import {
  _dropLiteStateCacheForTests,
  applyLiteProposal,
  getLiteProposal,
  getLiteServerState,
  hydrateLiteState,
  ignoreLiteProposal,
  performLiteAction,
  resetLiteForTests,
} from "./config-copilot-lite";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";
import { dispatchToTurnStore } from "./turn-store-dispatch";
import type { ConfigLiteProposePayload } from "./types";

const apiMock = vi.mocked(apiJson);

const APPEARANCE: ConfigLiteProposePayload = {
  proposal_id: "ccd_app",
  turn_id: "t_1",
  form_id: "user_appearance",
  workspace_id: null,
  workspace_name: null,
  rows: [
    { field: "mode", from: "light", to: "dark", from_unset: true },
    { field: "density", from: "comfortable", to: "compact", from_unset: false },
  ],
  notes: {},
};

const WS: ConfigLiteProposePayload = {
  proposal_id: "ccd_ws",
  turn_id: "t_1",
  form_id: "workspace_i18n",
  workspace_id: "ws_1",
  workspace_name: "商超零售",
  rows: [{ field: "week_start_day", from: "monday", to: "sunday", from_unset: true }],
  notes: {},
};

describe("personal appearance saved results", () => {
  it("uses server target receipts and never refreshes an unverified outcome", async () => {
    const saved = vi.fn();
    window.addEventListener(WORKSPACE_SETTINGS_SAVED_EVENT, saved);
    try {
      const proposal = { ...WS, team_id: "team_stale", workspace_id: "ws_stale" };
      apiMock.mockResolvedValueOnce(dto(WS.proposal_id, "applied"));
      expect(await performLiteAction(proposal, "apply")).toEqual(dto(WS.proposal_id, "applied"));
      expect((saved.mock.calls[0][0] as CustomEvent).detail.workspaceId).toBe("ws_1");
      expect((saved.mock.calls[0][0] as CustomEvent).detail.teamId).toBe("team_original");
      apiMock.mockResolvedValueOnce(dto(WS.proposal_id, "proposed"));
      await expect(performLiteAction(proposal, "apply")).rejects.toThrow("could not be verified");
      apiMock.mockRejectedValueOnce(conflictError("reverted"));
      expect(await performLiteAction(proposal, "apply")).toBeNull();
      expect(saved).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(WORKSPACE_SETTINGS_SAVED_EVENT, saved);
    }
  });

  it("notifies only the original Space after successful apply or undo", async () => {
    const saved = vi.fn();
    window.addEventListener(WORKSPACE_SETTINGS_SAVED_EVENT, saved);
    try {
      const proposal = { ...WS, team_id: "team_original" };
      apiMock.mockResolvedValueOnce(dto(WS.proposal_id, "applied"));
      await performLiteAction(proposal, "apply");
      expect((saved.mock.calls[0][0] as CustomEvent).detail).toEqual({
        teamId: "team_original",
        workspaceId: "ws_1",
        receipt: dto(WS.proposal_id, "applied"),
      });
      apiMock.mockResolvedValueOnce(dto(WS.proposal_id, "reverted"));
      await performLiteAction(proposal, "undo");
      expect(saved).toHaveBeenCalledTimes(2);
      apiMock.mockRejectedValueOnce(new Error("Save outcome unavailable"));
      await expect(performLiteAction(proposal, "apply")).rejects.toThrow("Save outcome unavailable");
      expect(saved).toHaveBeenCalledTimes(2);
    } finally {
      window.removeEventListener(WORKSPACE_SETTINGS_SAVED_EVENT, saved);
    }
  });

  it("retains the Team in replay and refreshes only after a successful action", async () => {
    dispatchToTurnStore("config.lite_propose", {
      proposal_id: "ccd_v3",
      turn_id: "t_1",
      form_id: "personal_appearance",
      team_id: "team_original",
      workspace_id: null,
      workspace_name: null,
      rows: APPEARANCE.rows,
      notes: {},
    });
    const proposal = getLiteProposal("ccd_v3")!;
    expect(proposal.team_id).toBe("team_original");
    const saved = vi.fn();
    window.addEventListener(PERSONAL_APPEARANCE_REFRESH_EVENT, saved);
    try {
      apiMock.mockResolvedValueOnce(dto("ccd_v3", "applied"));
      await performLiteAction(proposal, "apply");
      expect(saved).toHaveBeenCalledTimes(1);
      expect((saved.mock.calls[0][0] as CustomEvent).detail).toEqual({ teamId: "team_original" });
      expect(refreshEffectiveTheme).toHaveBeenLastCalledWith("team_original");
      apiMock.mockRejectedValueOnce(new Error("save failed"));
      await expect(performLiteAction(proposal, "undo")).rejects.toThrow("save failed");
      expect(saved).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(PERSONAL_APPEARANCE_REFRESH_EVENT, saved);
    }
  });
});

function dto(proposalId: string, status: string) {
  return {
    proposal_id: proposalId,
    form_id: proposalId === "ccd_ws" ? "workspace_i18n" : proposalId === "ccd_v3" ? "personal_appearance" : "user_appearance",
    status,
    workspace_id: proposalId === "ccd_ws" ? "ws_1" : null,
    team_id: "team_original",
    owner_user_id: "u_original",
  };
}

function conflictError(status: string): ApiError {
  return new ApiError(409, "conflict", {
    body: { detail: { error: { code: "proposal_state_conflict", details: { status } } } },
  });
}

beforeEach(() => {
  resetLiteForTests();
  _resetAllTurnsForTesting();
  apiMock.mockReset();
  vi.mocked(refreshEffectiveTheme).mockClear();
});

afterEach(() => {
  resetLiteForTests();
  _resetAllTurnsForTesting();
});

describe("dispatcher → lite beat", () => {
  const wire = (over: Record<string, unknown> = {}) => ({
    seq: 1,
    card_id: "t_1",
    proposal_id: "ccd_app",
    turn_id: "t_1",
    form_id: "user_appearance",
    workspace_id: null,
    workspace_name: null,
    rows: APPEARANCE.rows,
    notes: {},
    ...over,
  });

  it("合法 payload → beat + proposal store；同 proposalId 重放幂等", () => {
    expect(dispatchToTurnStore("config.lite_propose", wire())).toBe(true);
    expect(dispatchToTurnStore("config.lite_propose", wire({ seq: 2 }))).toBe(true);
    const beats = getTurnSnapshot("t_1")!.beats.filter((b) => b.kind === "lite");
    expect(beats).toHaveLength(1);
    expect(getLiteProposal("ccd_app")?.rows).toHaveLength(2);
  });

  it("坏 payload（缺 proposal_id / rows / turn 锚点）→ 丢弃不崩（claimed=true, 零 beat）", () => {
    expect(dispatchToTurnStore("config.lite_propose", wire({ proposal_id: undefined }))).toBe(true);
    expect(dispatchToTurnStore("config.lite_propose", wire({ rows: undefined }))).toBe(true);
    expect(dispatchToTurnStore("config.lite_propose", wire({ turn_id: undefined, card_id: undefined }))).toBe(true);
    expect(getTurnSnapshot("t_1")?.beats.filter((b) => b.kind === "lite") ?? []).toHaveLength(0);
  });
});

describe("hydrateLiteState", () => {
  it.each(["success", "not_found"])("preserves a newer Undo receipt after late hydration %s", async (outcome) => {
    let finish!: (value: unknown) => void;
    let reject!: (error: unknown) => void;
    apiMock.mockReturnValueOnce(new Promise((resolve, fail) => { finish = resolve; reject = fail; }));
    const pending = hydrateLiteState(WS.proposal_id);
    apiMock.mockResolvedValueOnce(dto(WS.proposal_id, "reverted"));
    await performLiteAction(WS, "undo");
    if (outcome === "success") finish(dto(WS.proposal_id, "applied"));
    else reject(new ApiError(404, "not found", { body: { detail: { error: { code: "config_proposal_not_found" } } } }));
    await pending;
    expect(getLiteServerState(WS.proposal_id)).toBe("reverted");
  });

  it("keeps a replacement hydration request attached when a pre-reset read settles", async () => {
    let finishOld!: (value: unknown) => void;
    let finishNew!: (value: unknown) => void;
    apiMock.mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve; }));
    const oldRead = hydrateLiteState(WS.proposal_id);
    _dropLiteStateCacheForTests();
    apiMock.mockReturnValueOnce(new Promise((resolve) => { finishNew = resolve; }));
    const newRead = hydrateLiteState(WS.proposal_id);
    finishOld(dto(WS.proposal_id, "applied"));
    await oldRead;
    expect(getLiteServerState(WS.proposal_id)).toBeNull();
    expect(hydrateLiteState(WS.proposal_id)).toBe(newRead);
    expect(apiMock).toHaveBeenCalledTimes(2);
    finishNew(dto(WS.proposal_id, "reverted"));
    await newRead;
    expect(getLiteServerState(WS.proposal_id)).toBe("reverted");
  });

  it("GET 终态 → cache；同 id 并发 hydrate 只发一次（in-flight 去重）；cache 命中零请求", async () => {
    let resolve: (v: unknown) => void = () => {};
    apiMock.mockReturnValueOnce(new Promise((r) => (resolve = r)) as Promise<unknown>);
    const first = hydrateLiteState("ccd_app");
    const second = hydrateLiteState("ccd_app");
    expect(apiMock).toHaveBeenCalledTimes(1);
    expect(apiMock.mock.calls[0][0]).toBe("/api/v1/config-proposals/ccd_app");
    resolve(dto("ccd_app", "applied"));
    await Promise.all([first, second]);
    expect(getLiteServerState("ccd_app")).toBe("applied");
    await hydrateLiteState("ccd_app");
    expect(apiMock).toHaveBeenCalledTimes(1);
  });

  it("结构化 404 → stale；裸 404（flag off）/网络错 → 不记 cache、可重试（R2 P2-3）", async () => {
    apiMock.mockRejectedValueOnce(new ApiError(404, "not found", { body: { detail: { error: { code: "config_proposal_not_found" } } } }));
    await hydrateLiteState("ccd_gone");
    expect(getLiteServerState("ccd_gone")).toBe("stale");

    // flag off: absent route yields FastAPI bare 404 (no structured code) → keep propose interactive (P3-2 semantics).
    apiMock.mockRejectedValueOnce(new ApiError(404, "Not Found", { body: { detail: "Not Found" } }));
    await hydrateLiteState("ccd_flagoff");
    expect(getLiteServerState("ccd_flagoff")).toBeNull();

    apiMock.mockRejectedValueOnce(new TypeError("network down"));
    await hydrateLiteState("ccd_net");
    expect(getLiteServerState("ccd_net")).toBeNull();
    apiMock.mockResolvedValueOnce(dto("ccd_net", "reverted"));
    await hydrateLiteState("ccd_net");
    expect(getLiteServerState("ccd_net")).toBe("reverted");
  });
});

describe("performLiteAction", () => {
  it("apply → 单次 POST /apply（零 settings API 调用）→ applied + user_appearance 主题刷新", async () => {
    applyLiteProposal(APPEARANCE);
    apiMock.mockResolvedValueOnce(dto("ccd_app", "applied"));
    await performLiteAction(APPEARANCE, "apply");
    expect(apiMock).toHaveBeenCalledTimes(1);
    expect(apiMock.mock.calls[0][0]).toBe("/api/v1/config-proposals/ccd_app/apply");
    expect(apiMock.mock.calls[0][1]).toEqual({ method: "POST" });
    expect(getLiteServerState("ccd_app")).toBe("applied");
    expect(refreshEffectiveTheme).toHaveBeenCalledTimes(1);
  });

  it("workspace 域 apply 不触发主题刷新；undo → POST /undo → reverted", async () => {
    apiMock.mockResolvedValueOnce({ ...dto("ccd_ws", "applied"), form_id: "workspace_i18n" });
    await performLiteAction(WS, "apply");
    expect(refreshEffectiveTheme).not.toHaveBeenCalled();
    apiMock.mockResolvedValueOnce({ ...dto("ccd_ws", "reverted"), form_id: "workspace_i18n" });
    await performLiteAction(WS, "undo");
    expect(apiMock.mock.calls[1][0]).toBe("/api/v1/config-proposals/ccd_ws/undo");
    expect(getLiteServerState("ccd_ws")).toBe("reverted");
  });

  it("user_appearance undo 也刷新主题（R2 P1-1：撤销外观后屏幕必须即刻回退）", async () => {
    apiMock.mockResolvedValueOnce(dto("ccd_app", "reverted"));
    await performLiteAction(APPEARANCE, "undo");
    expect(refreshEffectiveTheme).toHaveBeenCalledTimes(1);
  });

  it("409 state_conflict → 按服务端现态自愈不抛（跨设备另一端已 undo）", async () => {
    apiMock.mockRejectedValueOnce(conflictError("reverted"));
    await performLiteAction(APPEARANCE, "apply");
    expect(getLiteServerState("ccd_app")).toBe("reverted");
    expect(refreshEffectiveTheme).not.toHaveBeenCalled();
  });

  it("其他失败 → 抛给调用方（fail 态），状态不记", async () => {
    apiMock.mockRejectedValueOnce(new ApiError(503, "apply_failed"));
    await expect(performLiteAction(APPEARANCE, "apply")).rejects.toThrow();
    expect(getLiteServerState("ccd_app")).toBeNull();
  });
});

describe("ignoreLiteProposal", () => {
  it("POST /ignore → ignored；失败静默停留 propose", async () => {
    apiMock.mockResolvedValueOnce(dto("ccd_app", "ignored"));
    ignoreLiteProposal("ccd_app");
    await vi.waitFor(() => expect(getLiteServerState("ccd_app")).toBe("ignored"));
    expect(apiMock.mock.calls[0][0]).toBe("/api/v1/config-proposals/ccd_app/ignore");

    apiMock.mockRejectedValueOnce(new TypeError("net"));
    ignoreLiteProposal("ccd_p2");
    await new Promise((r) => setTimeout(r, 0));
    expect(getLiteServerState("ccd_p2")).toBeNull();
  });
});

describe("F5 语义", () => {
  it("清内存 cache 后重新 hydrate 恢复服务端终态（零 localStorage 依赖）", async () => {
    apiMock.mockResolvedValueOnce(dto("ccd_app", "applied"));
    await hydrateLiteState("ccd_app");
    expect(getLiteServerState("ccd_app")).toBe("applied");

    _dropLiteStateCacheForTests();
    expect(getLiteServerState("ccd_app")).toBeNull();
    apiMock.mockResolvedValueOnce(dto("ccd_app", "applied"));
    await hydrateLiteState("ccd_app");
    expect(getLiteServerState("ccd_app")).toBe("applied");
    // Batch 1-a: server-owned terminal states—zero localStorage references in module source (overlay removed entirely).
    const source = readFileSync(join(__dirname, "config-copilot-lite.ts"), "utf8");
    expect(source).not.toContain("localStorage");
  });
});

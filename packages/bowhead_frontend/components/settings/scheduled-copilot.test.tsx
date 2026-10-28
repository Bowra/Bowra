/** Configuration copilot × RoutineDrawer state-machine tests (PRD A5 latter half; R4 review P1-5).
 *
 * Guard applyDraft mapping, manual-edit protection (including generation-wait races = review P1-2), revert to
 * pre-AI baseline after consecutive generations (review P1-1), accept/acceptAll/clear semantics, zero copilot DOM with A4 flag off.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import type { ConfigDraftResult } from "@/lib/config-drafts-api";

const draftMock = vi.fn();
vi.mock("@/lib/config-drafts-api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/config-drafts-api")>();
  return { ...original, createConfigDraft: (...args: unknown[]) => draftMock(...args) };
});

// Drawer mount fetches bindable solidified workflows (scheduled-recipe-runs S1). Test copilot state machine only: empty
// list hides binding surface and prevents real network calls (unmocked also passes, but only because fetch fails).
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchBindableWorkflows: vi.fn(async () => ({ items: [] })),
}));

import { RoutineDrawer } from "./scheduled";

const t = (key: string, values?: Record<string, string | number>) => (values ? `${key}:${JSON.stringify(values)}` : key);

function result(fields: Record<string, unknown>, meta: Record<string, { needs_confirm?: boolean; note?: string | null }>, issues: ConfigDraftResult["issues"] = []): ConfigDraftResult {
  return {
    status: "ok",
    fields,
    field_meta: Object.fromEntries(
      Object.entries(meta).map(([k, v]) => [k, { origin: "generated" as const, needs_confirm: v.needs_confirm ?? false, note: v.note ?? null }]),
    ),
    issues,
    clarification: null,
    secret_redacted: false,
    form_id: "scheduled_job",
  };
}

const FULL = result(
  { name: "月度经营月报", query: "汇总上月经营情况", cron: "0 10 1 * *", tz: "Asia/Tokyo", notify_channels: { email: ["a@b.com"] }, workspace_id: "ws_1" },
  { name: {}, query: {}, cron: { needs_confirm: true, note: "平台暂不支持原周期" }, tz: {}, notify_channels: {} },
);

function setup() {
  return renderIntl(
    <RoutineDrawer mode="new" job={null} workspaces={[{ id: "ws_1", name: "空间A", desc: "" }]} t={t} onClose={vi.fn()} onSaved={vi.fn()} />,
  );
}

const nameInput = () => screen.getByPlaceholderText("field_name_placeholder") as HTMLInputElement;

const generateWith = async (payload: ConfigDraftResult | Promise<ConfigDraftResult>, text = "描述") => {
  draftMock.mockReturnValueOnce(Promise.resolve(payload));
  fireEvent.change(screen.getByPlaceholderText("用一句话描述这个任务，或粘贴相关资料…"), { target: { value: text } });
  fireEvent.click(screen.getByTestId("afp-generate"));
  await waitFor(() => expect(screen.queryByTestId("afp-sum")).toBeTruthy());
};

afterEach(() => {
  cleanup();
  draftMock.mockReset();
});

describe("RoutineDrawer × config-copilot", () => {
  it("applyDraft 映射：值落位 + ai/warn 态 + 月度 cron 走自定义 + tz 越界 fallback option", async () => {
    setup();
    await generateWith(FULL);
    expect(nameInput().value).toBe("月度经营月报");
    expect(document.querySelectorAll(".fld.ai").length).toBeGreaterThanOrEqual(3);
    expect(document.querySelector(".fld.warn")).toBeTruthy(); // cron needs_confirm
    expect(screen.getAllByText("平台暂不支持原周期").length).toBeGreaterThanOrEqual(1);
    expect((screen.getByPlaceholderText("0 9 * * *") as HTMLInputElement).value).toBe("0 10 1 * *");
    // review P1-3: Asia/Tokyo outside hardcoded four options must remain visible.
    const tzSelect = Array.from(document.querySelectorAll("select")).find((s) => s.value === "Asia/Tokyo");
    expect(tzSelect).toBeTruthy();
  });

  it("手改保护（review P1-2）：生成等待期打的字不被落地的响应覆盖", async () => {
    setup();
    let resolve!: (r: ConfigDraftResult) => void;
    draftMock.mockReturnValueOnce(new Promise<ConfigDraftResult>((r) => { resolve = r; }));
    fireEvent.change(screen.getByPlaceholderText("用一句话描述这个任务，或粘贴相关资料…"), { target: { value: "描述" } });
    fireEvent.click(screen.getByTestId("afp-generate"));
    // User manually fills name while waiting.
    fireEvent.change(nameInput(), { target: { value: "我自己的名字" } });
    resolve(FULL);
    await waitFor(() => expect(screen.queryByTestId("afp-sum")).toBeTruthy());
    expect(nameInput().value).toBe("我自己的名字"); // AI must not overwrite.
    expect(document.querySelector(".fld.kept")).toBeFalsy(); // Manual edit before generation has no badge (protected but unmarked).
    expect((screen.getByPlaceholderText("field_query_placeholder") as HTMLTextAreaElement).value).toBe("汇总上月经营情况"); // Other fields fill normally.
  });

  it("连续两次生成后还原（review P1-1）：回到 AI 前 baseline 而非第一代 AI 值", async () => {
    setup();
    await generateWith(result({ name: "A代", workspace_id: "ws_1" }, { name: {} }));
    await generateWith(result({ name: "B代", workspace_id: "ws_1" }, { name: {} }));
    expect(nameInput().value).toBe("B代");
    fireEvent.click(screen.getAllByText("还原")[0]);
    expect(nameInput().value).toBe(""); // baseline = before first AI fill (empty form).
    expect(document.querySelector(".fld.ai")).toBeFalsy();
  });

  it("accept→done、手改后 acceptAll/clear 均不动 kept 字段", async () => {
    setup();
    await generateWith(FULL);
    // accept name → done
    fireEvent.click(screen.getAllByText("接受")[0]);
    expect(document.querySelector(".fld.done")).toBeTruthy();
    // Manual query edit → kept badge.
    fireEvent.change(screen.getByPlaceholderText("field_query_placeholder"), { target: { value: "我的查询" } });
    expect(document.querySelector(".fld.kept")).toBeTruthy();
    // acceptAll: ai/warn → done, kept unchanged.
    fireEvent.click(screen.getByText("全部接受"));
    expect(document.querySelector(".fld.ai")).toBeFalsy();
    expect(document.querySelector(".fld.kept")).toBeTruthy();
    // clear: done returns to baseline, kept retains value.
    fireEvent.click(screen.getByText("清除建议"));
    expect(nameInput().value).toBe("");
    expect((screen.getByPlaceholderText("field_query_placeholder") as HTMLTextAreaElement).value).toBe("我的查询");
  });

  it("摘要计数走宿主实 admit 数（R4 P2-6）：生成前手改的字段不计入「已填写 n」", async () => {
    setup();
    fireEvent.change(nameInput(), { target: { value: "先手填的名字" } });
    await generateWith(FULL);
    // FULL has five generated meta entries, but protected kept name skips → actual admit four.
    expect(screen.getByTestId("afp-sum").textContent).toContain("已填写 4 个字段");
  });

  it("未映射字段的 issue 呈现（R4 P2-17）：workspace 意图未命中不再静默丢", async () => {
    setup();
    await generateWith(
      result(
        { name: "周报", workspace_id: "ws_1" },
        { name: {} },
        [{ field: "workspace_id", code: "workspace_intent_unresolved", message: "空间「营销」未找到或有歧义，已保持当前空间" }],
      ),
    );
    expect(screen.getByTestId("afp-notes").textContent).toContain("已保持当前空间");
  });

  // ── kept lifecycle guard (batch 0.5 §9.1 decision: protection lasts drawer mount lifetime; clearing suggestions retains it;
  //    closing/reopening drawer = fresh baseline/kept) ──
  it("kept 生命周期①：生成前手改的字段，生成不覆盖（keptRef 恒记录）", async () => {
    setup();
    fireEvent.change(nameInput(), { target: { value: "先手填的名字" } });
    await generateWith(FULL);
    expect(nameInput().value).toBe("先手填的名字");
    expect((screen.getByPlaceholderText("field_query_placeholder") as HTMLTextAreaElement).value).toBe("汇总上月经营情况");
  });

  it("kept 生命周期②：清除建议后 kept 值存活，且下一次生成仍受保护", async () => {
    setup();
    await generateWith(FULL);
    fireEvent.change(screen.getByPlaceholderText("field_query_placeholder"), { target: { value: "我的查询" } });
    fireEvent.click(screen.getByText("清除建议"));
    expect((screen.getByPlaceholderText("field_query_placeholder") as HTMLTextAreaElement).value).toBe("我的查询");
    await generateWith(FULL);
    expect((screen.getByPlaceholderText("field_query_placeholder") as HTMLTextAreaElement).value).toBe("我的查询");
    expect(nameInput().value).toBe("月度经营月报"); // Unprotected fields refill normally.
  });

  it("kept 生命周期③：关抽屉重开后保护重置，同字段可再被 AI 填", async () => {
    const first = setup();
    await generateWith(FULL);
    fireEvent.change(screen.getByPlaceholderText("field_query_placeholder"), { target: { value: "我的查询" } });
    first.unmount();

    setup();
    await generateWith(FULL);
    expect((screen.getByPlaceholderText("field_query_placeholder") as HTMLTextAreaElement).value).toBe("汇总上月经营情况");
  });

  // 2026-08-18: original A4 zero-copilot-DOM-when-off removed with FEATURE_CONFIG_COPILOT gate
  // (Allen decision: copilot always on for all customers). **No empty shell**: setting flagEnabled permanently true then asserting
  // no DOM when off can never fail, worse than no assertion: it implies protection still exists.
  // If flag returns, restore this zero-DOM-difference assertion together.
});

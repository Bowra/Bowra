/** DraftChip rendering in five states (tech-design-batch1b §5, test 12). */

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

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
const pushMock = vi.fn();
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: pushMock }) }));

import { apiJson } from "@/lib/api-client";
import { applyDraftEvent, resetDraftForTests } from "@/lib/config-copilot-draft";

import { DraftChip } from "./draft-chip";

const apiMock = vi.mocked(apiJson);

const PAYLOAD = {
  draft_id: "ccd_d1",
  turn_id: "t_1",
  form_id: "scheduled_job",
  created: true,
  updated_fields: ["name"],
  fields: { name: "销售周报" },
  field_meta: { name: { origin: "generated" as const, needs_confirm: false, note: null } },
  notes: {},
  workspace_id: "ws_1",
};

function dto(over: Record<string, unknown> = {}) {
  return {
    draft_id: "ccd_d1",
    form_id: "scheduled_job",
    status: "draft",
    workspace_id: "ws_1",
    fields: {},
    field_meta: {},
    notes: {},
    result: null,
    row_version: 1,
    ...over,
  };
}

beforeEach(() => {
  resetDraftForTests();
  apiMock.mockReset();
  pushMock.mockReset();
});

afterEach(() => {
  cleanup();
  resetDraftForTests();
});

describe("DraftChip", () => {
  it("shows a working-copy receipt without claiming the live agent changed", async () => {
    apiMock.mockResolvedValueOnce(dto({
      form_id: "team_subagent", status: "consumed",
      result: { kind: "team_subagent", stage: "working_copy", ref_id: "osa_1", label: "销售助手" },
    }));
    applyDraftEvent({ ...PAYLOAD, form_id: "team_subagent", target: { id: "osa_1", label: "销售助手" } });
    renderIntl(<DraftChip draftId="ccd_d1" onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("智能体「销售助手」草稿已保存，发布后才会生效")).toBeTruthy());
    fireEvent.click(screen.getByText("查看"));
    expect(pushMock).toHaveBeenCalledWith("/agents/manage/?agent_id=osa_1");
  });
  it("draft → 可打开的芯片，点击回调带 draftId", async () => {
    apiMock.mockResolvedValueOnce(dto());
    applyDraftEvent(PAYLOAD);
    const onOpen = vi.fn();
    renderIntl(<DraftChip draftId="ccd_d1" onOpen={onOpen} />);
    expect(screen.getByTestId("draft-chip")).toBeTruthy();
    fireEvent.click(screen.getByText("打开"));
    expect(onOpen).toHaveBeenCalledWith("ccd_d1");
  });

  it("consumed → 一行保存回执（含任务名 + 查看 + 下次运行）", async () => {
    apiMock.mockResolvedValueOnce(
      dto({ status: "consumed", result: { job_id: "sj_1", name: "销售周报", next_run_at: "2026-08-03T09:00:00+08:00" } }),
    );
    applyDraftEvent(PAYLOAD);
    renderIntl(<DraftChip draftId="ccd_d1" onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.queryByTestId("draft-receipt")).toBeTruthy());
    expect(screen.getByText("定时任务「销售周报」已创建")).toBeTruthy();
    fireEvent.click(screen.getByText("查看"));
    expect(pushMock).toHaveBeenCalledWith("/team/settings/scheduled");
  });

  it("consumed 但 result 缺失（防御）→ 通用回执，不崩", async () => {
    apiMock.mockResolvedValueOnce(dto({ status: "consumed", result: null }));
    applyDraftEvent(PAYLOAD);
    renderIntl(<DraftChip draftId="ccd_d1" onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.queryByTestId("draft-receipt")).toBeTruthy());
    expect(screen.getByText("定时任务已创建")).toBeTruthy();
  });

  it("discarded / expired 各留一行 muted 痕迹", async () => {
    apiMock.mockResolvedValueOnce(dto({ status: "discarded" }));
    applyDraftEvent(PAYLOAD);
    const first = renderIntl(<DraftChip draftId="ccd_d1" onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.queryByTestId("draft-discarded")).toBeTruthy());
    expect(screen.getByText("已放弃草稿")).toBeTruthy();
    first.unmount();

    resetDraftForTests();
    apiMock.mockResolvedValueOnce(dto({ draft_id: "ccd_d2", status: "expired" }));
    applyDraftEvent({ ...PAYLOAD, draft_id: "ccd_d2" });
    renderIntl(<DraftChip draftId="ccd_d2" onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.queryByTestId("draft-stale")).toBeTruthy());
    expect(screen.getByText("草稿已过期")).toBeTruthy();
  });

  it("payload 缺失（store 未 hydrate）→ 零渲染不崩", () => {
    apiMock.mockRejectedValueOnce(new TypeError("net"));
    const { container } = renderIntl(<DraftChip draftId="nope" onOpen={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });
});

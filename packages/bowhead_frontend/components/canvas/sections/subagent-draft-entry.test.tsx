import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

const listDrafts = vi.fn();
vi.mock("@/lib/subagent-drafts-api", () => ({
  listManualSubagentDrafts: (context: unknown) => listDrafts(context),
}));
const push = vi.fn();
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push, replace: vi.fn() }) }));
const auth = vi.hoisted(() => ({ me: { team_role: "member" } as Record<string, unknown> }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => auth }));

const SPACE = { teamId: "team_1", workspaceId: "ws_1" };

import { SubagentDraftEntry } from "./subagent-draft-entry";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("offers a specific existing task and a separate new task", async () => {
  listDrafts.mockResolvedValue([{
    draft_id: "sad_existing", title: "月报助手", source_mode: "manual", draft_status: "editing",
    updated_at: "2026-09-27T00:00:00Z", expires_at: "2026-10-01T00:00:00Z",
  }]);
  const onResume = vi.fn();
  const onNewManual = vi.fn();
  renderIntl(<SubagentDraftEntry requestContext={SPACE} spaceLoading={false} onResume={onResume} onNewAi={vi.fn()} onNewManual={onNewManual} />);

  expect((await screen.findByText(/手动草稿 · 编辑中 · 最近保存/)).textContent).toContain("有效期至");
  expect(screen.getByText(/手动草稿 · 编辑中/).textContent).toMatch(/年.*月.*日/);

  fireEvent.click(await screen.findByRole("button", { name: "继续" }));
  expect(onResume).toHaveBeenCalledWith("sad_existing");
  fireEvent.click(screen.getByRole("button", { name: "新建并手动填写" }));
  expect(onNewManual).toHaveBeenCalledOnce();
});

it("keeps new task actions available when the list is unavailable", async () => {
  listDrafts.mockRejectedValueOnce(new Error("offline"));
  listDrafts.mockResolvedValueOnce([]);
  const onNewAi = vi.fn();
  renderIntl(<SubagentDraftEntry requestContext={SPACE} spaceLoading={false} onResume={vi.fn()} onNewAi={onNewAi} onNewManual={vi.fn()} />);

  fireEvent.click(await screen.findByRole("button", { name: "描述需求，让 AI 起草" }));
  expect(onNewAi).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "重试读取" }));
  await waitFor(() => expect(listDrafts).toHaveBeenCalledTimes(2));
});

it("starts a new draft right away when there is no draft to continue", async () => {
  listDrafts.mockResolvedValue([]);
  const onNewManual = vi.fn();
  renderIntl(<SubagentDraftEntry unified requestContext={SPACE} spaceLoading={false} onResume={vi.fn()} onNewAi={vi.fn()} onNewManual={onNewManual} />);
  await waitFor(() => expect(onNewManual).toHaveBeenCalledOnce());
});

it("waits for a Space before reading drafts", async () => {
  listDrafts.mockResolvedValue([]);
  const onNewManual = vi.fn();
  const view = renderIntl(<SubagentDraftEntry unified requestContext={{ teamId: "team_1", workspaceId: "" }} spaceLoading
    onResume={vi.fn()} onNewAi={vi.fn()} onNewManual={onNewManual} />);
  expect(screen.getByLabelText("正在打开草稿…")).toBeTruthy();
  expect(listDrafts).not.toHaveBeenCalled();
  expect(onNewManual).not.toHaveBeenCalled();
  view.unmount();
});

it("explains that creation needs a Space when the account has none", async () => {
  auth.me = { team_role: "team_admin" };
  renderIntl(<SubagentDraftEntry unified requestContext={{ teamId: "team_1", workspaceId: "" }} spaceLoading={false}
    onResume={vi.fn()} onNewAi={vi.fn()} onNewManual={vi.fn()} />);
  expect(screen.getByText("先准备一个空间，再创建智能体")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "去创建空间" }));
  expect(push).toHaveBeenCalledWith("/team/settings/spaces/?from=no-space");
  expect(listDrafts).not.toHaveBeenCalled();
  auth.me = { team_role: "member" };
});

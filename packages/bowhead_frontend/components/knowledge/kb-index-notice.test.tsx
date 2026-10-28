import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { fetchLlmCatalog } from "@/lib/llm-config-api";
import { KbIndexNotice } from "./kb-index-notice";

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => navigation }));
vi.mock("@/lib/llm-config-api", () => ({ fetchLlmCatalog: vi.fn() }));

function show(canManage: boolean) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <KbIndexNotice canManage={canManage} />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => { vi.clearAllMocks(); });
afterEach(cleanup);

describe("knowledge index notice", () => {
  it("shows failed team index status to members without a management action", async () => {
    vi.mocked(fetchLlmCatalog).mockResolvedValue({
      version: 1, providers: [], kb: { reindex: { pending_embed: { state: "failed" } } },
    });
    show(false);
    expect((await screen.findByRole("alert")).textContent).toContain("团队知识库索引更新失败");
    expect(screen.getByText("请联系团队负责人或管理员处理。")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "查看并重试" })).toBeNull();
  });

  it("opens the index review for a team administrator", async () => {
    vi.mocked(fetchLlmCatalog).mockResolvedValue({
      version: 1, providers: [], kb: { reindex: { pending_embed: { state: "failed" } } },
    });
    show(true);
    fireEvent.click(await screen.findByRole("button", { name: "查看并重试" }));
    expect(navigation.push).toHaveBeenCalledWith("/knowledge/index/");
  });

  it("stays quiet when no index rebuild is pending", async () => {
    vi.mocked(fetchLlmCatalog).mockResolvedValue({ version: 1, providers: [], kb: { reindex: { state: "idle", pending_embed: null } } });
    show(true);
    await waitFor(() => expect(fetchLlmCatalog).toHaveBeenCalledOnce());
    expect(screen.queryByTestId("kb-index-notice")).toBeNull();
  });
});

/**
 * Redirect bookmarks and old links for /knowledge/doc-types/records/?dt=… to the knowledge base Extraction Results view.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ replace: vi.fn(), search: "dt=dt_1" }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(mocks.search),
}));

import KnowledgeRecordsRedirectPage from "./page";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("旧提取结果页地址", () => {
  it("带 dt：落到该提取表的结果", async () => {
    mocks.search = "dt=dt_1";
    render(<KnowledgeRecordsRedirectPage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/knowledge/?tab=records&view=tpl_dt_1"));
  });

  it("不带 dt：落到「提取结果」列表", async () => {
    mocks.search = "";
    render(<KnowledgeRecordsRedirectPage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/knowledge/?tab=records"));
  });
});

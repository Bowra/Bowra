/**
 * Redirect bookmarks and old links for /knowledge/doc-types/ to the knowledge base Templates view (a canonical URL with the URI prefix).
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace, push: vi.fn() }),
}));

import KnowledgeDocTypesRedirectPage from "./page";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("旧提取表管理页地址", () => {
  it("重定向到知识库「模板」视图", async () => {
    render(<KnowledgeDocTypesRedirectPage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/knowledge/?tab=templates"));
  });
});

/**
 * Redirect bookmarks and old links for /knowledge/doc-types/ to the knowledge base Templates view (a canonical URL with the URI prefix).
 * With knowledge bases enabled, extraction tables live under the Extraction tables tab instead.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  replace: vi.fn(),
  session: undefined as undefined | { status: string; me: { capabilities?: { knowledge_bases?: boolean } } | null },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace, push: vi.fn() }),
}));

vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => mocks.session,
}));

import KnowledgeDocTypesRedirectPage from "./page";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mocks.session = undefined;
});

describe("旧提取表管理页地址", () => {
  it("重定向到知识库「模板」视图", async () => {
    render(<KnowledgeDocTypesRedirectPage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/knowledge/?tab=templates"));
  });

  it("switch off: still redirects to Templates once the session is known", async () => {
    mocks.session = { status: "authenticated", me: { capabilities: { knowledge_bases: false } } };
    render(<KnowledgeDocTypesRedirectPage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/knowledge/?tab=templates"));
  });

  it("switch on: redirects to the Extraction tables tab, and waits while the session loads", async () => {
    mocks.session = { status: "loading", me: null };
    const view = render(<KnowledgeDocTypesRedirectPage />);
    expect(mocks.replace).not.toHaveBeenCalled();
    mocks.session = { status: "authenticated", me: { capabilities: { knowledge_bases: true } } };
    view.rerender(<KnowledgeDocTypesRedirectPage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/knowledge/?tab=records"));
    expect(mocks.replace).toHaveBeenCalledTimes(1);
  });
});

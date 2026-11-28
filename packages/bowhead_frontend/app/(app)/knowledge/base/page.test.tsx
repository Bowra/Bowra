/**
 * /knowledge/base/ route (FEATURE_KNOWLEDGE_BASES): reads `?id=&tab=&file_id=&chunk_id=&space=` for `KbPage` and waits
 * for the session. Unknown tabs open Files; the whole query is the file table's view state.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messagesZh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  search: "",
  auth: { status: "authed", me: { id: "u_me" }, isTeamAdmin: false, error: null } as { status: string; me: unknown; isTeamAdmin: boolean; error: null },
  props: [] as Array<Record<string, unknown>>,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(mocks.search),
}));
vi.mock("@/lib/auth/use-auth-redirect", () => ({
  useAuthRedirect: () => mocks.auth,
}));
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/knowledge/bases/kb-page", () => ({
  KbPage: (props: Record<string, unknown>) => {
    mocks.props.push(props);
    return <div data-testid="kb-page-stub" />;
  },
}));

import KnowledgeBaseRoutePage from "./page";

function renderRoute(search: string) {
  mocks.search = search;
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <KnowledgeBaseRoutePage />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  mocks.props = [];
  mocks.auth = { status: "authed", me: { id: "u_me" }, isTeamAdmin: false, error: null };
});

afterEach(cleanup);

describe("/knowledge/base/ route", () => {
  it("passes the knowledge base, tab, file, chunk and Space from the address", async () => {
    renderRoute("id=kbase_ops&tab=members&file_id=kbf_1&chunk_id=c_7&space=ws_main&q=abc");
    expect(await screen.findByTestId("kb-page-stub")).toBeInTheDocument();
    expect(mocks.props.at(-1)).toEqual({
      knowledgeBaseId: "kbase_ops",
      tab: "members",
      fileId: "kbf_1",
      chunkId: "c_7",
      spaceId: "ws_main",
      viewQuery: "id=kbase_ops&tab=members&file_id=kbf_1&chunk_id=c_7&space=ws_main&q=abc",
    });
  });

  it("opens Files for a missing or unknown tab and passes file-only links without an ID", async () => {
    renderRoute("tab=bogus&file_id=kbf_1");
    await screen.findByTestId("kb-page-stub");
    expect(mocks.props.at(-1)).toMatchObject({ knowledgeBaseId: null, tab: "files", fileId: "kbf_1", chunkId: null, spaceId: null });
  });

  it("waits for the session with the page title and a loading state", () => {
    mocks.auth = { status: "loading", me: null, isTeamAdmin: false, error: null };
    renderRoute("id=kbase_ops");
    expect(screen.queryByTestId("kb-page-stub")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(messagesZh.knowledge_base.title);
  });
});

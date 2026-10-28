import { act, renderHook } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AGENT_CREATION_HANDOFF_KEY } from "./agent-creation-handoff";
import { useAgentDraftUrl } from "./use-agent-draft-url";

afterEach(() => {
  window.history.replaceState(null, "", "/");
  sessionStorage.clear();
});

type Props = {
  search: string;
  cardOpen: boolean;
  authoringEnabled?: boolean;
  scope?: { teamId: string | null; workspaceId: string | null; sessionId: string | null };
};

function setup(initial: Props) {
  const onRestoreDraft = vi.fn();
  const onStartCreation = vi.fn();
  const onScopeChange = vi.fn();
  const hook = renderHook(
    ({ search, cardOpen, scope, authoringEnabled = true }: Props) => useAgentDraftUrl({
      // The page uses Next useSearchParams: after writing a URL without the marker, it follows the URL the page itself wrote.
      searchParams: new URLSearchParams(search),
      authoringEnabled,
      cardOpen,
      onRestoreDraft,
      onStartCreation,
      scope,
      onScopeChange,
    }),
    { initialProps: initial },
  );
  return { hook, onRestoreDraft, onStartCreation, onScopeChange };
}

describe("useAgentDraftUrl", () => {
  it("does not treat Strict Mode's initial effect replay as closing a restored draft", () => {
    window.history.replaceState(null, "", "/?agent_draft=sad_strict");
    const onRestoreDraft = vi.fn();
    renderHook(() => useAgentDraftUrl({ searchParams: new URLSearchParams("?agent_draft=sad_strict"),
      authoringEnabled: true, cardOpen: false, onRestoreDraft, onStartCreation: vi.fn() }),
    { wrapper: StrictMode });
    expect(onRestoreDraft).toHaveBeenCalledExactlyOnceWith("sad_strict");
    expect(window.location.search).toBe("?agent_draft=sad_strict");
  });

  it.each(["teamId", "workspaceId", "sessionId"] as const)("detaches the old draft when %s changes and still permits an explicit deep link", (field) => {
    const scope = { teamId: "team_a", workspaceId: "ws_a", sessionId: "s_a" };
    const { hook, onScopeChange, onRestoreDraft } = setup({ search: "", cardOpen: true, scope });
    act(() => hook.result.current.showDraftInUrl("sad_old"));
    hook.rerender({ search: "?agent_draft=sad_old", cardOpen: true, scope });
    const nextScope = { ...scope, [field]: `${scope[field]}_next` };
    hook.rerender({ search: "?agent_draft=sad_old", cardOpen: true, scope: nextScope });
    expect(onScopeChange).toHaveBeenCalledOnce();
    expect(window.location.search).toBe("");
    expect(onRestoreDraft).not.toHaveBeenCalled();

    window.history.pushState(null, "", "/?agent_draft=sad_next");
    hook.rerender({ search: "?agent_draft=sad_next", cardOpen: false, scope: nextScope });
    expect(onRestoreDraft).toHaveBeenCalledExactlyOnceWith("sad_next");
  });

  it("preserves a draft while the first message assigns its session", () => {
    const scope = { teamId: "team_a", workspaceId: "ws_a", sessionId: null };
    const { hook, onScopeChange } = setup({ search: "", cardOpen: true, scope });
    act(() => hook.result.current.showDraftInUrl("sad_first"));
    hook.rerender({ search: "?agent_draft=sad_first", cardOpen: true, scope: { ...scope, sessionId: "s_first" } });
    expect(onScopeChange).not.toHaveBeenCalled();
    expect(window.location.search).toBe("?agent_draft=sad_first");
  });

  it("keeps a refresh deep link while authentication and the initial Space resolve", () => {
    window.history.replaceState(null, "", "/?session=s_first&agent_draft=sad_first");
    const initial = { search: "?session=s_first&agent_draft=sad_first", cardOpen: false, authoringEnabled: false,
      scope: { teamId: null, workspaceId: null, sessionId: null } };
    const { hook, onRestoreDraft, onScopeChange } = setup(initial);
    hook.rerender({ ...initial, authoringEnabled: true, scope: { ...initial.scope, teamId: "team_a" } });
    hook.rerender({ ...initial, cardOpen: true, authoringEnabled: true,
      scope: { teamId: "team_a", workspaceId: "ws_a", sessionId: "s_first" } });
    expect(onRestoreDraft).toHaveBeenCalledExactlyOnceWith("sad_first");
    expect(onScopeChange).not.toHaveBeenCalled();
    expect(window.location.search).toContain("agent_draft=sad_first");
  });

  it("restores a second explicit draft without clearing its URL on the previous card's close", () => {
    window.history.replaceState(null, "", "/?agent_draft=sad_first");
    const { hook, onRestoreDraft } = setup({ search: "?agent_draft=sad_first", cardOpen: false });
    hook.rerender({ search: "?agent_draft=sad_first", cardOpen: true });
    window.history.pushState(null, "", "/?agent_draft=sad_second");
    hook.rerender({ search: "?agent_draft=sad_second", cardOpen: false });
    expect(onRestoreDraft.mock.calls).toEqual([["sad_first"], ["sad_second"]]);
    expect(window.location.search).toBe("?agent_draft=sad_second");
  });

  it("waits for authoring availability before consuming a saved creation handoff", () => {
    sessionStorage.setItem(AGENT_CREATION_HANDOFF_KEY, "Review weekly sales");
    const { hook, onStartCreation } = setup({ search: "", cardOpen: false, authoringEnabled: false });
    expect(onStartCreation).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(AGENT_CREATION_HANDOFF_KEY)).toBe("Review weekly sales");
    hook.rerender({ search: "", cardOpen: false, authoringEnabled: true });
    expect(onStartCreation).toHaveBeenCalledExactlyOnceWith("Review weekly sales");
  });

  it("带着草稿号打开页面 → 恢复这份草稿", () => {
    window.history.replaceState(null, "", "/?agent_draft=sad_arrival");
    const { onRestoreDraft } = setup({ search: "?agent_draft=sad_arrival", cardOpen: false });
    expect(onRestoreDraft).toHaveBeenCalledExactlyOnceWith("sad_arrival");
  });

  it("页面自己写下草稿号后 searchParams 的回声不重置卡片（草稿版本等进行中的状态不丢）", () => {
    const { hook, onRestoreDraft } = setup({ search: "", cardOpen: true });
    act(() => hook.result.current.showDraftInUrl("sad_mine"));
    expect(window.location.search).toBe("?agent_draft=sad_mine");
    hook.rerender({ search: "?agent_draft=sad_mine", cardOpen: true });
    // Later URL synchronization (e.g. session parameters) may change searchParams again; this is still an echo.
    hook.rerender({ search: "?session=s2&agent_draft=sad_mine", cardOpen: true });
    expect(onRestoreDraft).not.toHaveBeenCalled();
  });

  it("negative control：同样的草稿号不是页面写的 → 按软导航恢复", () => {
    const { hook, onRestoreDraft } = setup({ search: "", cardOpen: false });
    window.history.pushState(null, "", "/?agent_draft=sad_other");
    hook.rerender({ search: "?agent_draft=sad_other", cardOpen: false });
    expect(onRestoreDraft).toHaveBeenCalledExactlyOnceWith("sad_other");
  });

  it("卡片不经「清草稿号」关闭 → 页面写下的草稿号一并清掉，之后软导航到同一草稿照常恢复", () => {
    const { hook, onRestoreDraft } = setup({ search: "", cardOpen: true });
    act(() => hook.result.current.showDraftInUrl("sad_x"));
    hook.rerender({ search: "?agent_draft=sad_x", cardOpen: true });
    hook.rerender({ search: "?agent_draft=sad_x", cardOpen: false });
    expect(window.location.search).toBe("");
    hook.rerender({ search: "", cardOpen: false });
    expect(onRestoreDraft).not.toHaveBeenCalled();

    window.history.pushState(null, "", "/?agent_draft=sad_x");
    hook.rerender({ search: "?agent_draft=sad_x", cardOpen: false });
    expect(onRestoreDraft).toHaveBeenCalledExactlyOnceWith("sad_x");
  });

  it("?agent_create=1 或会话存储交接 → 打开创建入口，并清掉入口参数", () => {
    window.history.replaceState(null, "", "/?agent_create=1");
    const { onStartCreation } = setup({ search: "?agent_create=1", cardOpen: false });
    expect(onStartCreation).toHaveBeenCalledExactlyOnceWith(null);
    expect(window.location.search).toBe("");

    // Remove only the entry parameter; preserve the path (including deployment prefix), session and other parameters unchanged.
    window.history.replaceState(null, "", "/bowhead/?session=s2&agent_create=1&keep=1#top");
    const withSession = setup({ search: "?session=s2&agent_create=1&keep=1", cardOpen: false });
    expect(withSession.onStartCreation).toHaveBeenCalledExactlyOnceWith(null);
    expect(`${window.location.pathname}${window.location.search}${window.location.hash}`).toBe("/bowhead/?session=s2&keep=1#top");

    window.history.replaceState(null, "", "/");
    sessionStorage.setItem(AGENT_CREATION_HANDOFF_KEY, "月度销售复盘");
    const second = setup({ search: "", cardOpen: false });
    expect(second.onStartCreation).toHaveBeenCalledExactlyOnceWith("月度销售复盘");
  });
});

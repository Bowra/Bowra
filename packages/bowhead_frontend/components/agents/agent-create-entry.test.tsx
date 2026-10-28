import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { COMPOSER_PREFILL_KEY } from "@/lib/composer-handoff";
import { AGENT_CREATION_HANDOFF_KEY } from "@/lib/agent-creation-handoff";
import { AgentCreateEntry } from "./agent-create-entry";

const replace = vi.fn();
const push = vi.fn();
const useAuthSession = vi.fn();
const spaceSelection = vi.fn(() => ({ loading: false, error: null as string | null, activeId: "ws_a" as string | null }));
const peekFeedbackPrefill = vi.fn();
const clearFeedbackPrefill = vi.fn();

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ replace, push }),
}));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => spaceSelection(),
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useAuthSession: () => useAuthSession(),
}));
vi.mock("@/lib/feedback-candidates-api", () => ({
  peekFeedbackPrefill: (...args: unknown[]) => peekFeedbackPrefill(...args),
  clearFeedbackPrefill: (...args: unknown[]) => clearFeedbackPrefill(...args),
}));

describe("AgentCreateEntry", () => {
  it("explains that creation needs a Space instead of silently returning Home", async () => {
    spaceSelection.mockReturnValue({ loading: false, error: null, activeId: null });
    useAuthSession.mockReturnValue({ me: { team_role: "owner", capabilities: { subagent_creator: true } } });
    const view = render(<AgentCreateEntry />);
    expect(view.getByText("create_no_space_title")).toBeTruthy();
    expect(view.getByText("create_no_space_body_manage")).toBeTruthy();
    view.getByRole("button", { name: "create_no_space_action" }).click();
    expect(push).toHaveBeenCalledWith("/team/settings/spaces/?from=no-space");
    expect(replace).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(AGENT_CREATION_HANDOFF_KEY)).toBeNull();
  });

  it("tells a member without a Space whom to ask, with no settings link", () => {
    spaceSelection.mockReturnValue({ loading: false, error: null, activeId: null });
    useAuthSession.mockReturnValue({ me: { team_role: "member", capabilities: { subagent_creator: true } } });
    const view = render(<AgentCreateEntry />);
    expect(view.getByText("create_no_space_body_member")).toBeTruthy();
    expect(view.queryByRole("button", { name: "create_no_space_action" })).toBeNull();
  });

  afterEach(() => {
    cleanup();
    spaceSelection.mockReturnValue({ loading: false, error: null, activeId: "ws_a" });
    vi.unstubAllGlobals();
    sessionStorage.clear();
    window.history.replaceState(null, "", "/agents/new/");
    vi.clearAllMocks();
    vi.restoreAllMocks();
  });

  it("carries an adopted Agent purpose into the right-side creation flow", async () => {
    window.history.replaceState(null, "", "/agents/new/?feedback_prefill=1&ref=new");
    useAuthSession.mockReturnValue({
      me: { capabilities: { subagent_creator: true } },
    });
    peekFeedbackPrefill.mockReturnValue({
      prefill: { kind: "cca_intent", purpose: "整理会议行动项" },
    });

    render(<AgentCreateEntry />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    expect(peekFeedbackPrefill).toHaveBeenCalledWith("new");
    expect(sessionStorage.getItem(AGENT_CREATION_HANDOFF_KEY)).toBe("整理会议行动项");
    expect(sessionStorage.getItem(COMPOSER_PREFILL_KEY)).toBeNull();
    expect(clearFeedbackPrefill).toHaveBeenCalledTimes(1);
  });

  it("opens an empty right-side creation flow without a handoff purpose", async () => {
    useAuthSession.mockReturnValue({
      me: { capabilities: { subagent_creator: true } },
    });

    render(<AgentCreateEntry />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    expect(sessionStorage.getItem(AGENT_CREATION_HANDOFF_KEY)).toBe("");
    expect(sessionStorage.getItem(COMPOSER_PREFILL_KEY)).toBeNull();
  });

  it("keeps the direct creation entry usable when session storage is denied", async () => {
    useAuthSession.mockReturnValue({
      me: { capabilities: { subagent_creator: true } },
    });
    vi.stubGlobal("sessionStorage", {
      setItem: () => { throw new Error("storage denied"); },
    });

    render(<AgentCreateEntry />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/?agent_create=1"));
  });

  it("returns a direct deep link to the market when creation is disabled", async () => {
    useAuthSession.mockReturnValue({
      me: { capabilities: { subagent_creator: false } },
    });

    render(<AgentCreateEntry />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/agents/"));
    expect(sessionStorage.getItem(COMPOSER_PREFILL_KEY)).toBeNull();
  });
});

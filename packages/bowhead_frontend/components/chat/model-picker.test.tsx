import { createRef } from "react";
import { headersExactly } from "@/__tests__/request-headers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const apiMocks = vi.hoisted(() => ({
  getUserSettings: vi.fn(),
  setUserSetting: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => {
    const messages: Record<string, string> = {
      active: "Active",
      clear: "Use Team Default",
      loading: "Loading",
      load_failed: "Load failed",
      no_providers: "No providers",
      no_providers_hint: "No providers are configured",
      no_providers_hint_manage: "Add a model connection in Team settings",
      team_default: "Team default",
      stale_selection: "Selection is no longer available",
    };
    return messages[key] ?? key;
  },
}));

const sessionMock = vi.hoisted(() => ({ role: "member" as string }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { team_role: sessionMock.role } }),
}));

vi.mock("@/lib/api", () => ({
  getUserSettings: apiMocks.getUserSettings,
  setUserSetting: apiMocks.setUserSetting,
}));

import { ModelPicker } from "./model-picker";

const ORIGINAL_FETCH = globalThis.fetch;

function installMemStorage() {
  let store: Record<string, string> = {};
  const mem: Storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    clear: () => { store = {}; },
    key: (i) => Object.keys(store)[i] ?? null,
    get length() { return Object.keys(store).length; },
  };
  Object.defineProperty(window, "localStorage", { value: mem, configurable: true });
}

function catalogResp(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

const CATALOG = {
  version: 7,
  providers: [
    {
      id: "openai_main",
      display_name: "OpenAI Main",
      type: "openai_compatible",
      models: ["gpt-4.1-mini", "gpt-4.1"],
    },
    {
      id: "anthropic_main",
      display_name: "Anthropic Main",
      type: "anthropic_compatible",
      models: ["claude-3-5-sonnet-latest"],
    },
  ],
  default_model: { provider: "openai_main", model: "gpt-4.1-mini" },
};

beforeEach(() => {
  installMemStorage();
  window.localStorage.clear();
  apiMocks.getUserSettings.mockResolvedValue({});
  apiMocks.setUserSetting.mockResolvedValue({});
  globalThis.fetch = vi.fn(async () => catalogResp(CATALOG)) as typeof fetch;
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

describe("ModelPicker", () => {
  it.each([["owner", "Add a model connection in Team settings"], ["member", "No providers are configured"]])(
    "tells a %s without models what they can do",
    async (role, hint) => {
      sessionMock.role = role;
      globalThis.fetch = vi.fn(async () => catalogResp({ version: 1, providers: [], default_model: null })) as typeof fetch;
      render(<ModelPicker />);
      expect((await screen.findByRole("button", { name: /No providers/ })).getAttribute("title")).toBe(hint);
      sessionMock.role = "member";
    },
  );

  it("updates the displayed and checked model after a selection in another tab", async () => {
    render(<ModelPicker />);
    await screen.findByRole("button", { name: /gpt-4\.1-mini/ });
    apiMocks.setUserSetting.mockClear();

    const selection = "anthropic_main:claude-3-5-sonnet-latest";
    window.localStorage.setItem("bowhead.preferred_model", selection);
    fireEvent(window, new StorageEvent("storage", {
      key: "bowhead.preferred_model",
      newValue: selection,
    }));

    fireEvent.click(await screen.findByRole("button", { name: /claude-3-5-sonnet-latest/ }));
    expect(screen.getByRole("menuitemradio", { name: /claude-3-5-sonnet-latest/ }).getAttribute("aria-checked")).toBe("true");
    expect(apiMocks.setUserSetting).not.toHaveBeenCalled();
  });

  it.each(["bowhead.preferred_model", null])("shows the team default after another tab clears the preference (key: %s)", async (key) => {
    const selection = "anthropic_main:claude-3-5-sonnet-latest";
    window.localStorage.setItem("bowhead.preferred_model", selection);
    apiMocks.getUserSettings.mockResolvedValue({ preferred_model: selection });
    render(<ModelPicker />);
    await screen.findByRole("button", { name: /claude-3-5-sonnet-latest/ });
    await waitFor(() => expect(apiMocks.getUserSettings).toHaveBeenCalled());
    apiMocks.setUserSetting.mockClear();

    window.localStorage.removeItem("bowhead.preferred_model");
    fireEvent(window, new StorageEvent("storage", { key, oldValue: selection }));

    fireEvent.click(await screen.findByRole("button", { name: /gpt-4\.1-mini/ }));
    expect(screen.getByRole("menuitemradio", { name: /Use Team Default/ }).getAttribute("aria-checked")).toBe("true");
    expect(apiMocks.setUserSetting).not.toHaveBeenCalled();
  });

  it("names only the model on the trigger and its hint, never the provider id", async () => {
    render(<ModelPicker />);
    const trigger = await screen.findByRole("button", { name: /gpt-4\.1-mini/ });
    expect(trigger.getAttribute("title")).toBe("gpt-4.1-mini (Team default)");
    expect(document.querySelector("[role='tooltip']")?.textContent).toBe("gpt-4.1-mini (Team default)");
    expect(document.body.textContent).not.toContain("openai_main");
  });

  it("loads the team catalog and persists the selected provider/model", async () => {
    render(<ModelPicker />);

    const trigger = await screen.findByRole("button", { name: /gpt-4\.1-mini/ });
    fireEvent.click(trigger);
    expect(await screen.findByRole("menu")).toBeTruthy();

    fireEvent.click(screen.getByRole("menuitemradio", { name: /claude-3-5-sonnet-latest/ }));

    expect(window.localStorage.getItem("bowhead.preferred_model")).toBe(
      "anthropic_main:claude-3-5-sonnet-latest",
    );
    await waitFor(() => {
      expect(apiMocks.setUserSetting).toHaveBeenCalledWith(
        "preferred_model",
        "anthropic_main:claude-3-5-sonnet-latest",
      );
    });
    expect(globalThis.fetch).toHaveBeenCalledWith("/api/v1/llm/catalog", {
      credentials: "include",
      headers: headersExactly({}),
    });
  });

  it("clears a stale local preferred model that is not in the team catalog", async () => {
    window.localStorage.setItem("bowhead.preferred_model", "old_provider:old_model");
    apiMocks.getUserSettings.mockResolvedValue({ preferred_model: "old_provider:old_model" });

    render(<ModelPicker />);

    await waitFor(() => {
      expect(window.localStorage.getItem("bowhead.preferred_model")).toBeNull();
    });
    expect(apiMocks.setUserSetting).toHaveBeenCalledWith("preferred_model", "");

    fireEvent.click(await screen.findByRole("button", { name: /gpt-4\.1-mini/ }));
    expect(await screen.findByText("Selection is no longer available")).toBeTruthy();
  });

  it("mousedown on a portaled menu item does NOT close the menu (selection survives)", async () => {
    // Regression: the menu is portaled to document.body (outside wrapRef). The click-outside
    // handler runs on mousedown, which in a real browser precedes click. Without excluding the
    // portaled menu (menuRef), mousedown on a menu item would setOpen(false) → unmount → the
    // selecting click never fires. The original test used synthetic click only, missing this.
    render(<ModelPicker />);
    fireEvent.click(await screen.findByRole("button", { name: /gpt-4\.1-mini/ }));
    const item = await screen.findByRole("menuitemradio", { name: /claude-3-5-sonnet-latest/ });
    fireEvent.mouseDown(item); // real pointer sequence: mousedown bubbles to the window handler
    expect(screen.queryByRole("menu")).toBeTruthy(); // menu must stay open
    fireEvent.click(item); // the click that actually selects
    expect(window.localStorage.getItem("bowhead.preferred_model")).toBe(
      "anthropic_main:claude-3-5-sonnet-latest",
    );
  });

  it("returns focus to the composer fallback when the open picker unmounts", async () => {
    const fallbackRef = createRef<HTMLTextAreaElement>();
    const { rerender } = render(
      <>
        <textarea ref={fallbackRef} aria-label="Composer input" />
        <ModelPicker fallbackFocusRef={fallbackRef} />
      </>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /gpt-4\.1-mini/ }));
    const menuItem = await screen.findByRole("menuitemradio", { name: /claude-3-5-sonnet-latest/ });
    menuItem.focus();
    expect(document.activeElement).toBe(menuItem);

    rerender(<textarea ref={fallbackRef} aria-label="Composer input" />);

    await waitFor(() => expect(document.activeElement).toBe(fallbackRef.current));
  });

  it("focuses the currently selected model when the menu opens", async () => {
    render(<ModelPicker />);
    fireEvent.click(await screen.findByRole("button", { name: /gpt-4\.1-mini/ }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: /claude-3-5-sonnet-latest/ }));

    fireEvent.click(await screen.findByRole("button", { name: /claude-3-5-sonnet-latest/ }));
    const active = await screen.findByRole("menuitemradio", { name: /claude-3-5-sonnet-latest/ });
    expect(active.getAttribute("aria-checked")).toBe("true");

    await waitFor(() => expect(document.activeElement).toBe(active));
  });
});

describe("ModelPicker menu behaviour", () => {
  it("moves between models with the arrow keys", async () => {
    render(<ModelPicker />);
    fireEvent.click(await screen.findByRole("button", { name: /gpt-4\.1-mini/ }), { detail: 0 });
    const items = await screen.findAllByRole("menuitemradio");
    await waitFor(() => expect(document.activeElement).toBe(items[0]));

    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "End" });
    expect(document.activeElement?.textContent).toContain("claude-3-5-sonnet-latest");
  });

  it("closes on Escape and returns focus to the trigger", async () => {
    render(<ModelPicker />);
    const trigger = await screen.findByRole("button", { name: /gpt-4\.1-mini/ });
    fireEvent.click(trigger, { detail: 0 });
    const menu = await screen.findByRole("menu");
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));

    fireEvent.keyDown(menu, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("closes on a press outside", async () => {
    render(
      <>
        <p>Outside</p>
        <ModelPicker />
      </>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /gpt-4\.1-mini/ }));
    expect(await screen.findByRole("menu")).toBeTruthy();

    fireEvent.pointerDown(screen.getByText("Outside"));

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("closes when the composer disables it and hands focus to the composer input", async () => {
    const fallbackRef = createRef<HTMLTextAreaElement>();
    const view = (disabled: boolean) => (
      <>
        <textarea ref={fallbackRef} aria-label="Composer input" />
        <ModelPicker disabled={disabled} fallbackFocusRef={fallbackRef} />
      </>
    );
    const { rerender } = render(view(false));
    fireEvent.click(await screen.findByRole("button", { name: /gpt-4\.1-mini/ }), { detail: 0 });
    const menu = await screen.findByRole("menu");
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));

    rerender(view(true));

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(fallbackRef.current));
  });
});

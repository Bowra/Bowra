/**
 * Space picker: space-list loading failures explain only what to do; status codes stay off the page and go into Copy Diagnostics.
 * Exercise the real chain (fetchWorkspaces → useSpace → SpacePicker), replacing only fetch.
 */
import "@testing-library/jest-dom/vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { invalidateSpaces } from "@/lib/use-space";

import { SpacePicker } from "./space-picker";

const REQUEST_ID = "0123456789abcdef0123456789abcdef";

function stubWorkspaces(respond: () => Promise<Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) =>
      String(input).includes("/api/v1/auth/me/workspaces")
        ? respond()
        : Promise.resolve(new Response("{}", { status: 404 })),
    ),
  );
}

async function openMenu(trigger: string) {
  const button = await screen.findByRole("button", { name: new RegExp(trigger, "i") });
  await waitFor(() => expect(button).not.toBeDisabled());
  fireEvent.click(button, { detail: 0 });
  return screen.findByRole("menu");
}

afterEach(() => {
  act(() => invalidateSpaces());
  vi.unstubAllGlobals();
});

describe("SpacePicker load failure (real fetchWorkspaces → useSpace chain)", () => {
  it.each([
    ["zh-CN", "切换空间", "没能加载空间列表。请刷新页面后再试。", "复制诊断"],
    ["en-US", "Switch space", "Couldn't load your spaces. Refresh the page and try again.", "Copy diagnostics"],
  ] as const)("explains what to do and offers Copy diagnostics as a menu item (%s)", async (locale, trigger, sentence, copy) => {
    stubWorkspaces(async () => new Response("{}", { status: 500, headers: { "x-request-id": REQUEST_ID } }));
    renderIntl(<SpacePicker />, { locale });

    const menu = await openMenu(trigger);

    expect(await screen.findByText(sentence)).toBeInTheDocument();
    const item = await screen.findByRole("menuitem", { name: copy });
    expect(menu.contains(item)).toBe(true);
    expect(document.body.textContent).not.toMatch(/500|workspaces:|0123456789abcdef/);
    // Arrow keys can reach Copy Diagnostics.
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(item));
  });

  it("copies the status and request id from the menu item and reports the result", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    stubWorkspaces(async () => new Response("{}", { status: 500, headers: { "x-request-id": REQUEST_ID } }));
    renderIntl(<SpacePicker />);

    await openMenu("切换空间");
    fireEvent.click(await screen.findByRole("menuitem", { name: "复制诊断" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const bundle = String(writeText.mock.calls[0][0]);
    expect(bundle).toContain("http_status: 500");
    expect(bundle).toContain(`request_id: ${REQUEST_ID}`);
  });

  it("renders no empty diagnostics entry when the request never reached the server", async () => {
    stubWorkspaces(async () => {
      throw new TypeError("Failed to fetch");
    });
    renderIntl(<SpacePicker />);

    const menu = await openMenu("切换空间");

    expect(await screen.findByText("没能加载空间列表。请刷新页面后再试。")).toBeInTheDocument();
    expect(screen.queryByRole("menuitem")).toBeNull();
    expect(menu.querySelectorAll(".legacy-menu-space-note")).toHaveLength(1);
  });
});

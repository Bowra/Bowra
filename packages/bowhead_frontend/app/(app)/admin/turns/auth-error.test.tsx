/**
 * Turn replay: permission-check failures only explain what to do; status codes stay off the page and Copy Diagnostics is outside the announcement container.
 * Exercise the real chain (fetchMe → AuthSessionProvider errorCause → useAuthRedirect → page), replacing only fetch.
 */
import "@testing-library/jest-dom/vitest";
import { screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { AuthSessionProvider } from "@/lib/auth/auth-session-context";

vi.mock("@/components/admin-shell", () => ({ AdminShell: ({ children }: { children: ReactNode }) => <>{children}</> }));

import TurnsPage from "./page";

const REQUEST_ID = "0123456789abcdef0123456789abcdef";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Turn replay access check failure (real session chain)", () => {
  it.each([
    ["zh-CN", "无法确认访问权限。请刷新页面", "复制诊断"],
    ["en-US", "Unable to verify access. Refresh the page.", "Copy diagnostics"],
  ] as const)("explains what to do and offers Copy diagnostics outside the alert (%s)", async (locale, sentence, copy) => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) =>
        Promise.resolve(
          String(input).includes("/api/v1/auth/me")
            ? new Response(JSON.stringify({ detail: "upstream unavailable" }), {
                status: 502,
                headers: { "content-type": "application/json", "x-request-id": REQUEST_ID },
              })
            : new Response("{}", { status: 404 }),
        ),
      ),
    );

    renderIntl(<AuthSessionProvider><TurnsPage /></AuthSessionProvider>, { locale });

    const button = await screen.findByRole("button", { name: copy });
    const alert = screen.getByRole("alert");
    await waitFor(() => expect(alert).toHaveTextContent(sentence));
    expect(alert.contains(button)).toBe(false);
    expect(document.body.textContent).not.toMatch(/502|upstream unavailable|0123456789abcdef/);
  });
});

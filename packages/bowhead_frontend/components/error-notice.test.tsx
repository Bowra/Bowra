import { NextIntlClientProvider } from "next-intl";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import enMessages from "@/messages/en-US.json";
import zhMessages from "@/messages/zh-CN.json";

import { ErrorNotice } from "./error-notice";

const { capability } = vi.hoisted(() => ({
  capability: { enabled: false },
}));

vi.mock("@/lib/use-error-diagnostics-capability", () => ({
  useErrorDiagnosticsCapability: () => capability.enabled,
}));

const DIAGNOSTICS = {
  schemaVersion: 1,
  disclosure: "public-neutral",
  status: 421,
  code: "public_entry_not_registered",
  diagnosticCode: "public_entry_registry_miss",
  requestId: "0123456789abcdef0123456789abcdef",
  retryable: false,
  observedAt: "2026-07-29T10:30:00.000Z",
} as const;

describe("ErrorNotice", () => {
  beforeEach(() => {
    capability.enabled = false;
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_VERSION", "1.2.3");
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("keeps the localized main message when the feature is off", () => {
    renderNotice("en-US");

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("This address is not registered.");
    expect(screen.queryByTestId("error-diagnostics")).toBeNull();
    expect(screen.queryByText(DIAGNOSTICS.requestId)).toBeNull();
  });

  it("renders one live region and a separate native disclosure", () => {
    capability.enabled = true;
    renderNotice("en-US");

    const alert = screen.getByRole("alert");
    const disclosure = screen.getByTestId("error-diagnostics");
    expect(alert.textContent).toContain("This address is not registered.");
    expect(within(alert).queryByText("View technical details")).toBeNull();
    expect(disclosure.tagName).toBe("DETAILS");
    expect(disclosure.hasAttribute("open")).toBe(false);
    expect(
      screen
        .getByText(DIAGNOSTICS.requestId)
        .closest("code")
        ?.getAttribute("translate"),
    ).toBe("no");
  });

  it("copies only the fixed diagnostic projection and reports success", async () => {
    capability.enabled = true;
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderNotice("en-US");

    fireEvent.click(
      screen.getByRole("button", {
        name: "Copy diagnostic information",
      }),
    );
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toContain("Bowhead error diagnostic v1");
    expect(copied).toContain("surface: login");
    expect(copied).toContain("version: 1.2.3");
    expect(copied).toContain(`request_id: ${DIAGNOSTICS.requestId}`);
    expect(copied).not.toContain("window.location");
    expect(screen.getByRole("status").textContent).toContain(
      "Diagnostic information copied",
    );
  });

  it("renders the disclosure controls in Chinese", () => {
    capability.enabled = true;
    renderNotice("zh-CN");

    expect(screen.getByText("查看技术详情")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "复制诊断信息" }),
    ).toBeTruthy();
    expect(screen.getByText("是否可重试")).toBeTruthy();
    expect(screen.getByText("否")).toBeTruthy();
  });
});

function renderNotice(locale: "en-US" | "zh-CN") {
  const messages = locale === "en-US" ? enMessages : zhMessages;
  return render(
    <NextIntlClientProvider locale={locale} messages={messages}>
      <ErrorNotice
        message={
          locale === "en-US"
            ? "This address is not registered."
            : "当前访问地址尚未登记。"
        }
        diagnostics={DIAGNOSTICS}
        surface="login"
      />
    </NextIntlClientProvider>,
  );
}

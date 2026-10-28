/** Diagnostics beside error text: Copy Diagnostics alone with the flag off, a collapsible area with it on; Toast uses a Copy Diagnostics action. */
import "@testing-library/jest-dom/vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { HttpStatusError } from "@/lib/api-client";

const state = vi.hoisted(() => ({ copy: vi.fn(), toast: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyTextWithFallback: (...args: unknown[]) => state.copy(...args) }));
vi.mock("@/lib/use-toast", async (orig) => ({ ...(await orig<Record<string, unknown>>()), toast: (...args: unknown[]) => state.toast(...args) }));

import { ErrorDiagnosticsInline, useCopyDiagnosticsToastAction } from "./error-notice";

const REQUEST_ID = "0123456789abcdef0123456789abcdef";
const failure = () =>
  Object.assign(new HttpStatusError("reports", new Response(null, { status: 503, headers: { "x-request-id": REQUEST_ID } })), {
    code: "reports_unavailable",
  });

afterEach(() => {
  state.copy.mockReset();
  state.toast.mockReset();
});

describe("ErrorDiagnosticsInline", () => {
  it.each([
    ["zh-CN", "复制诊断", "已复制"],
    ["en-US", "Copy diagnostics", "Copied"],
  ] as const)("diagnostics off: a small Copy diagnostics button, no status code on the page (%s)", async (locale, label, done) => {
    state.copy.mockResolvedValue(true);
    renderIntl(<ErrorDiagnosticsInline error={failure()} surface="route" diagnosticsEnabled={false} />, { locale });

    expect(document.body.textContent).not.toMatch(/503|reports_unavailable|0123456789abcdef/);
    fireEvent.click(screen.getByRole("button", { name: label }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(done));
    const bundle = String(state.copy.mock.calls[0][0]);
    expect(bundle).toContain("http_status: 503");
    expect(bundle).toContain("error_code: reports_unavailable");
    expect(bundle).toContain(`request_id: ${REQUEST_ID}`);
    expect(screen.queryByTestId("error-diagnostics")).toBeNull();
  });

  it("says Couldn't copy when copying fails", async () => {
    state.copy.mockResolvedValue(false);
    renderIntl(<ErrorDiagnosticsInline error={failure()} surface="route" diagnosticsEnabled={false} />);

    fireEvent.click(screen.getByRole("button", { name: "复制诊断" }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("复制失败"));
  });

  it("diagnostics on: keeps the disclosure instead of the quick button", () => {
    renderIntl(<ErrorDiagnosticsInline error={failure()} surface="route" diagnosticsEnabled />);

    expect(screen.getByTestId("error-diagnostics")).toHaveTextContent("503");
    expect(screen.queryByTestId("error-copy-diagnostics")).toBeNull();
  });

  it("renders nothing when the failure carries no diagnostics", () => {
    const { container } = renderIntl(<ErrorDiagnosticsInline error={new Error("boom")} surface="route" diagnosticsEnabled={false} />);

    expect(container).toBeEmptyDOMElement();
  });
});

describe("useCopyDiagnosticsToastAction", () => {
  function Probe({ error, onReady }: { error: unknown; onReady: (action: unknown) => void }) {
    onReady(useCopyDiagnosticsToastAction()(error, "route"));
    return null;
  }

  it("gives the toast a Copy diagnostics action that reports the result", async () => {
    state.copy.mockResolvedValue(true);
    let action: { label: string; onAction: () => Promise<void> } | undefined;
    renderIntl(<Probe error={failure()} onReady={(value) => { action = value as typeof action; }} />);

    expect(action?.label).toBe("复制诊断");
    await action?.onAction();
    expect(String(state.copy.mock.calls[0][0])).toContain("http_status: 503");
    expect(state.toast).toHaveBeenCalledWith(expect.objectContaining({ kind: "success", text: "已复制" }));
  });

  it("adds no action when there is nothing to copy", () => {
    let action: unknown = "unset";
    renderIntl(<Probe error={new Error("boom")} onReady={(value) => { action = value; }} />);

    expect(action).toBeUndefined();
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ApiError } from "@/lib/api-client";
import { llmMaxRetriesFromConfig } from "@/lib/llm-config-api";

import { parseRetryDraft, RetrySettingSection } from "./retry-setting-section";

const saveAdminLlmRetry = vi.fn();

vi.mock("@/lib/llm-config-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/llm-config-api")>()),
  saveAdminLlmRetry: (...args: unknown[]) => saveAdminLlmRetry(...args),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vals?: Record<string, unknown>) =>
    vals ? `${key}:${Object.values(vals).join(",")}` : key,
}));

beforeEach(() => saveAdminLlmRetry.mockReset());
afterEach(cleanup);

function renderSection(over: Partial<Parameters<typeof RetrySettingSection>[0]> = {}) {
  const onSaved = vi.fn();
  render(
    <RetrySettingSection
      value={3}
      version={7}
      onSaved={onSaved}
      userFacingError={() => "generic"}
      {...over}
    />,
  );
  return { onSaved, input: screen.getByLabelText("label") as HTMLInputElement, save: screen.getByRole("button", { name: "save" }) };
}

describe("saved value", () => {
  it("defaults to 3 when the configuration has no retry setting", () => {
    expect(llmMaxRetriesFromConfig({})).toBe(3);
    expect(llmMaxRetriesFromConfig({ retry: { max_retries: 9 } })).toBe(3);
    expect(llmMaxRetriesFromConfig({ retry: { max_retries: 0 } })).toBe(0);
    expect(llmMaxRetriesFromConfig({ retry: { max_retries: 5 } })).toBe(5);
  });

  it("accepts only whole numbers from 0 to 5", () => {
    expect(parseRetryDraft("0")).toBe(0);
    expect(parseRetryDraft(" 5 ")).toBe(5);
    expect(parseRetryDraft("6")).toBeNull();
    expect(parseRetryDraft("-1")).toBeNull();
    expect(parseRetryDraft("1.5")).toBeNull();
    expect(parseRetryDraft("")).toBeNull();
  });
});

describe("RetrySettingSection", () => {
  it("saves with the loaded version as expected_version and reports the new version", async () => {
    saveAdminLlmRetry.mockResolvedValue({ ok: true, version: 8, retry: { max_retries: 1 } });
    const { input, save, onSaved } = renderSection();
    expect(input.value).toBe("3");
    expect((save as HTMLInputElement | HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: "1" } });
    fireEvent.click(save);
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith({ ok: true, version: 8, retry: { max_retries: 1 } }));
    expect(saveAdminLlmRetry).toHaveBeenCalledWith(1, 7);
  });

  it("states the allowed range for an out-of-range value and does not send it", () => {
    const { input, save } = renderSection();
    fireEvent.change(input, { target: { value: "8" } });
    expect(screen.getByRole("alert").textContent).toBe("error_range:0,5");
    expect((save as HTMLInputElement | HTMLButtonElement).disabled).toBe(true);
    expect(saveAdminLlmRetry).not.toHaveBeenCalled();
  });

  it("asks to reload when the configuration changed since the page loaded", async () => {
    saveAdminLlmRetry.mockRejectedValueOnce(new ApiError(409, "conflict", { code: "config_version_conflict" }));
    const { input, save, onSaved } = renderSection();
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.click(save);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("error_stale"));
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("explains a server-side range rejection and a missing model configuration", async () => {
    saveAdminLlmRetry.mockRejectedValueOnce(new ApiError(400, "bad", { code: "invalid_llm_retry" }));
    const { input, save } = renderSection();
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.click(save);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("error_range:0,5"));

    saveAdminLlmRetry.mockRejectedValueOnce(new ApiError(409, "none", { code: "llm_config_not_configured" }));
    fireEvent.click(save);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("error_not_configured"));
  });

  it("is disabled while the rest of the page is locked", () => {
    const { input, save } = renderSection({ disabled: true });
    expect((input as HTMLInputElement | HTMLButtonElement).disabled).toBe(true);
    expect((save as HTMLInputElement | HTMLButtonElement).disabled).toBe(true);
  });
});

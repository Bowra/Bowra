import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api-client";
import type { ExecutionLimits } from "@/lib/execution-limits-api";

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
const getMock = vi.fn();
const saveMock = vi.fn();
vi.mock("@/lib/execution-limits-api", () => ({ getExecutionLimits: (...args: unknown[]) => getMock(...args), saveExecutionLimits: (...args: unknown[]) => saveMock(...args) }));
import { ExecutionLimitsSettings } from "./execution-limits";

const values = { sandbox_default_timeout_s: 120, sandbox_max_timeout_s: 600, html_max_bytes: 26214400 };
const initial: ExecutionLimits = { ...values, defaults: values, version: "default", is_default: true, ranges: {
  sandbox_default_timeout_s: { min: 5, max: 600 }, sandbox_max_timeout_s: { min: 5, max: 600 }, html_max_bytes: { min: 1, max: 52428800 },
} };

beforeEach(() => getMock.mockResolvedValue(initial));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("ExecutionLimitsSettings", () => {
  it("saves displayed MiB as bytes with the loaded version", async () => {
    saveMock.mockResolvedValue({ ...initial, sandbox_default_timeout_s: 240, html_max_bytes: 8388608, version: "v2", is_default: false });
    render(<ExecutionLimitsSettings />);
    await screen.findByLabelText("html_max_bytes_title");
    fireEvent.change(screen.getByLabelText("html_max_bytes_title"), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("sandbox_default_timeout_s_title"), { target: { value: "240" } });
    fireEvent.click(screen.getByRole("button", { name: "save" }));
    await screen.findByText("saved");
    expect(saveMock.mock.calls[0][0]).toEqual({ ...values, sandbox_default_timeout_s: 240, html_max_bytes: 8388608 });
    expect(saveMock.mock.calls[0][1]).toBe("default");
  });

  it("blocks a default duration above the Team maximum", async () => {
    render(<ExecutionLimitsSettings />);
    await screen.findByLabelText("sandbox_max_timeout_s_title");
    fireEvent.change(screen.getByLabelText("sandbox_max_timeout_s_title"), { target: { value: "60" } });
    expect(screen.getByText("error_default_range")).toBeTruthy();
    expect((screen.getByRole("button", { name: "save" }) as HTMLButtonElement).disabled).toBe(true);
    expect(saveMock).not.toHaveBeenCalled();
  });

  it("stages inherited defaults for review before saving", async () => {
    getMock.mockResolvedValue({ ...initial, html_max_bytes: 8388608, is_default: false, version: "v2" });
    saveMock.mockResolvedValue({ ...initial, version: "v3" });
    render(<ExecutionLimitsSettings />);
    await screen.findByLabelText("html_max_bytes_title");
    fireEvent.click(screen.getByRole("button", { name: "reset" }));
    expect((screen.getByLabelText("html_max_bytes_title") as HTMLInputElement).value).toBe("25");
    expect(saveMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1));
    expect(saveMock.mock.calls[0].slice(0, 2)).toEqual([null, "v2"]);
  });

  it("keeps a conflicting draft until the administrator explicitly reloads", async () => {
    saveMock.mockRejectedValue(new ApiError(409, "Version conflict"));
    render(<ExecutionLimitsSettings />);
    await screen.findByLabelText("html_max_bytes_title");
    fireEvent.change(screen.getByLabelText("html_max_bytes_title"), { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: "save" }));
    await screen.findByText("error_conflict");
    expect((screen.getByLabelText("html_max_bytes_title") as HTMLInputElement).value).toBe("8");
    getMock.mockResolvedValue({ ...initial, html_max_bytes: 10485760, version: "v2", is_default: false });
    fireEvent.click(screen.getByRole("button", { name: "reload" }));
    await waitFor(() => expect((screen.getByLabelText("html_max_bytes_title") as HTMLInputElement).value).toBe("10"));
  });
});

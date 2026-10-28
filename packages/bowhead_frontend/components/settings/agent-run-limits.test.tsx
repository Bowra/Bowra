import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";
import type { AgentRunLimits } from "@/lib/agent-run-limits-api";

const t = (key: string, values?: Record<string, unknown>) =>
  values ? `${key}:${JSON.stringify(values)}` : key;
vi.mock("next-intl", () => ({ useTranslations: () => t }));


const saveMock = vi.fn();
const resetMock = vi.fn();
vi.mock("@/lib/agent-run-limits-api", () => ({
  saveAgentRunLimits: (...args: unknown[]) => saveMock(...args),
  resetAgentRunLimits: (...args: unknown[]) => resetMock(...args),
}));

import { AgentRunLimitsSettings } from "./agent-run-limits";

const limits = (patch: Partial<AgentRunLimits> = {}): AgentRunLimits => ({
  turnTimeLimitS: 1800,
  stepLimit: null,
  toolCallLimit: 300,
  wrapUpReserveS: 300,
  isDefault: true,
  version: "default",
  defaults: { turnTimeLimitS: 1800, stepLimit: null, toolCallLimit: 300 },
  ranges: {
    turnTimeLimitS: { min: 600, max: 36000 },
    stepLimit: { min: 1, max: 10000 },
    toolCallLimit: { min: 1, max: 10000 },
  },
  ...patch,
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AgentRunLimitsSettings", () => {
  it("saves an explicit model call deadline and allows following the turn again", async () => {
    const initial = limits({ modelCallTimeLimitS: null, ranges: {
      ...limits().ranges, modelCallTimeLimitS: { min: 30, max: 36000 },
    } });
    saveMock.mockResolvedValue({ ...initial, modelCallTimeLimitS: 900, isDefault: false, version: "v2" });
    render(<AgentRunLimitsSettings initial={initial} />);
    expect(screen.getByRole("radio", { name: "model_call_automatic" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getAllByRole("radio", { name: "steps_at_most" })[0]);
    fireEvent.change(screen.getByLabelText("model_call_input_label"), { target: { value: "900" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1));
    expect(saveMock.mock.calls[0][0].modelCallTimeLimitS).toBe(900);
    await screen.findByText("saved");
    fireEvent.click(screen.getByRole("radio", { name: "model_call_automatic" }));
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(2));
    expect(saveMock.mock.calls[1][0].modelCallTimeLimitS).toBeNull();
  });

  it("shows the defaults with unlimited steps and the step input disabled", () => {
    render(<AgentRunLimitsSettings initial={limits()} />);
    expect((screen.getByLabelText("turn_time_title") as HTMLInputElement).value).toBe("30");
    expect(screen.getByRole("radio", { name: "steps_unlimited" }).getAttribute("aria-checked")).toBe("true");
    expect((screen.getByLabelText("steps_input_label") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("default_badge")).toBeTruthy();
  });

  it("saves minutes as seconds and a step limit, then reports it applies from the next turn", async () => {
    saveMock.mockResolvedValue(limits({ turnTimeLimitS: 7200, stepLimit: 200, isDefault: false, version: "v2" }));
    render(<AgentRunLimitsSettings initial={limits()} />);
    fireEvent.change(screen.getByLabelText("turn_time_title"), { target: { value: "120" } });
    fireEvent.click(screen.getByRole("radio", { name: "steps_at_most" }));
    fireEvent.change(screen.getByLabelText("steps_input_label"), { target: { value: "200" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1));
    expect(saveMock.mock.calls[0][0]).toEqual({ turnTimeLimitS: 7200, stepLimit: 200, toolCallLimit: 300 });
    expect(saveMock.mock.calls[0][1]).toBe("default");
    await screen.findByText("saved");
  });

  it("names the field and range and blocks saving when a value is out of range", () => {
    render(<AgentRunLimitsSettings initial={limits()} />);
    fireEvent.change(screen.getByLabelText("turn_time_title"), { target: { value: "5" } });
    expect(screen.getByText('error_range:{"field":"turn_time_title","min":10,"max":600}')).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(saveMock).not.toHaveBeenCalled();
  });

  it("keeps the admin's values and explains a concurrent edit", async () => {
    saveMock.mockRejectedValue(new ApiError(409, "conflict", { code: "agent_run_limits_conflict" }));
    render(<AgentRunLimitsSettings initial={limits()} />);
    fireEvent.change(screen.getByLabelText("tool_calls_title"), { target: { value: "500" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await screen.findByText("error_conflict");
    expect((screen.getByLabelText("tool_calls_title") as HTMLInputElement).value).toBe("500");
  });
});

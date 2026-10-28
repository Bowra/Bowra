import { useState } from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/messages/en-US.json";
import { RuntimeParametersSection } from "./runtime-parameters-section";
import type {
  ParameterPreview,
  ParameterProfile,
  RuntimeParameters,
} from "@/lib/runtime-parameters";

const profiles: ParameterProfile[] = [
  {
    id: "dashscope-mixed-chat-v1",
    label: "DashScope Chat",
    family: "openai",
    provider_types: ["openai_compatible"],
    thinking_modes: ["off", "on", "omit"],
    efforts: [],
    budget_min: 1,
    budget_max: 32768,
    temperature_max: 2,
    source: "https://help.aliyun.com/zh/model-studio/deep-thinking",
  },
];
const preview: ParameterPreview = {
  capability: "chat",
  synthetic: true,
  provider_request_count: 0,
  configuration_writes: 0,
  parameters: [
    { field: "enable_thinking", value: false, source: "model_default" },
  ],
};

function setup(onPreview = vi.fn().mockResolvedValue(preview)) {
  function Harness() {
    const [draft, setDraft] = useState<{
      runtimeParameters?: RuntimeParameters;
      requestBodyOverrides?: string;
      parameterError?: string;
    }>({
      runtimeParameters: { profile: profiles[0].id },
      requestBodyOverrides: "{}",
    });
    return (
      <NextIntlClientProvider locale="en-US" messages={messages}>
        <RuntimeParametersSection
          index={0}
          modelId="m"
          protocol="openai_compatible"
          capabilities={["chat", "tool", "vision"]}
          profiles={profiles}
          runtime={draft.runtimeParameters}
          legacyParams={{}}
          legacyRaw={draft.requestBodyOverrides || "{}"}
          previewKey={JSON.stringify(draft)}
          onChange={(patch) => setDraft((old) => ({ ...old, ...patch }))}
          onPreview={onPreview}
        />
        <output data-testid="policy">{JSON.stringify(draft)}</output>
        <button disabled={Boolean(draft.parameterError)}>Save fixture</button>
      </NextIntlClientProvider>
    );
  }
  const rendered = render(<Harness />);
  act(() => {
    const details = screen.getByTestId(
      "runtime-parameters-0",
    ) as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event("toggle"));
  });
  return { ...rendered, onPreview };
}

describe("shared defaults and capability overrides", () => {
  it("changes only one Tool field and preserves it after a default edit", () => {
    setup();
    fireEvent.change(screen.getByLabelText("Thinking mode"), {
      target: { value: "off" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Capability overrides (0)" }),
    );
    fireEvent.change(screen.getByLabelText("Thinking mode"), {
      target: { value: "on" },
    });
    const policy = JSON.parse(screen.getByTestId("policy").textContent || "{}");
    expect(policy.runtimeParameters.defaults).toEqual({ thinking_mode: "off" });
    expect(policy.runtimeParameters.capability_overrides).toEqual({
      tool: { thinking_mode: "on" },
    });
    fireEvent.change(screen.getByLabelText("Thinking mode"), {
      target: { value: "follow" },
    });
    expect(
      JSON.parse(screen.getByTestId("policy").textContent || "{}")
        .runtimeParameters.capability_overrides,
    ).toBeUndefined();
  });

  it("uses a boolean selector and round-trips explicit false", () => {
    setup();
    fireEvent.change(
      screen.getByLabelText("Parallel tool calls — Request parameters"),
      { target: { value: "fixed" } },
    );
    fireEvent.change(
      screen.getByLabelText("Parallel tool calls — Boolean value"),
      { target: { value: "false" } },
    );
    expect(
      JSON.parse(screen.getByTestId("policy").textContent || "{}")
        .runtimeParameters.defaults.outbound_params.parallel_tool_calls.value,
    ).toBe(false);
    expect(screen.queryByRole("spinbutton", { name: /Parallel/ })).toBeNull();
  });

  it("keeps invalid advanced text and blocks saving instead of losing the draft", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    fireEvent.change(
      screen.getByLabelText("Advanced parameter JSON for this scope"),
      { target: { value: '{"seed":1,"seed":2}' } },
    );
    expect(
      screen
        .getByRole("button", { name: "Save fixture" })
        .hasAttribute("disabled"),
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Fields" }));
    expect(
      (
        screen.getByLabelText(
          "Advanced parameter JSON for this scope",
        ) as HTMLTextAreaElement
      ).value,
    ).toBe('{"seed":1,"seed":2}');
  });

  it("never previews on open and discards a result for an older draft", async () => {
    let finish!: (result: ParameterPreview) => void;
    const onPreview = vi.fn(
      () =>
        new Promise<ParameterPreview>((resolve) => {
          finish = resolve;
        }),
    );
    setup(onPreview);
    expect(onPreview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Refresh preview" }));
    fireEvent.change(screen.getByLabelText("Thinking mode"), {
      target: { value: "off" },
    });
    await act(async () => finish(preview));
    await waitFor(() =>
      expect(screen.queryByText("enable_thinking")).toBeNull(),
    );
  });
});

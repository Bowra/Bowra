import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { ContextPolicy } from "@/lib/context-policy";
import { ContextPolicyFields } from "./context-policy-fields";

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/components/settings/configuration-field-help", () => ({ ConfigurationFieldHelp: () => null }));

function Editor({ initial }: { initial?: ContextPolicy }) {
  const [value, setValue] = useState(initial);
  return <><ContextPolicyFields index={0} value={value} onChange={setValue} /><output data-testid="saved">{JSON.stringify(value) ?? "unset"}</output></>;
}

describe("context policy settings", () => {
  it("defaults on without saving an inferred preference", () => {
    render(<Editor />);
    expect(screen.getByRole("switch", { name: "enabled" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByTestId("saved").textContent).toBe("unset");
  });

  it("keeps numeric preferences when toggled off and supports an explicit whole-object reset", () => {
    render(<Editor initial={{ trigger_input_tokens: 90_000, keep_recent_tokens: 20_000 }} />);
    fireEvent.click(screen.getByRole("switch", { name: "enabled" }));
    expect(JSON.parse(screen.getByTestId("saved").textContent!)).toEqual({ enabled: false, trigger_input_tokens: 90_000, keep_recent_tokens: 20_000 });
    fireEvent.click(screen.getByText("advanced"));
    fireEvent.click(screen.getByText("reset"));
    expect(screen.getByTestId("saved").textContent).toBe("unset");
    expect(screen.getByRole("switch", { name: "enabled" }).getAttribute("aria-checked")).toBe("true");
  });

  it("shows an actionable invalid-combination error without coercing the draft", () => {
    render(<Editor initial={{ enabled: false, trigger_input_tokens: 1000, keep_recent_tokens: 2000 }} />);
    expect(screen.getByRole("alert").textContent).toBe("invalid");
    expect(JSON.parse(screen.getByTestId("saved").textContent!).enabled).toBe(false);
  });
});

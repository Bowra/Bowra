import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/settings/settings-shell", () => ({
  SettingsShell: ({
    active,
    children,
  }: {
    active: string;
    children: React.ReactNode;
  }) => (
    <div data-testid="settings-shell" data-active={active}>
      {children}
    </div>
  ),
}));

import { MemoryShell } from "./memory-shell";

describe("MemoryShell", () => {
  afterEach(cleanup);

  it("embeds Memory in personal settings", () => {
    render(<MemoryShell>Memory</MemoryShell>);

    expect(screen.getByTestId("settings-shell").dataset.active).toBe("memory");
  });
});

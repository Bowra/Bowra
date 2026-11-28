import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { ShareConsequences } from "./agent-config-v2";

describe("ShareConsequences", () => {
  it("offers the member link next to the no-notification bullet when the Agent is shared", () => {
    const onCopy = vi.fn();
    renderIntl(<ShareConsequences mode="custom" copyLink={{ label: "Copy link for members", onCopy }} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy link for members" }));
    expect(onCopy).toHaveBeenCalledTimes(1);
  });

  it("keeps the private note without a link", () => {
    renderIntl(<ShareConsequences mode="private" copyLink={{ label: "Copy link for members", onCopy: vi.fn() }} />);
    expect(screen.queryByRole("button", { name: "Copy link for members" })).toBeNull();
  });
});

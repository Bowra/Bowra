import { describe, expect, it } from "vitest";

import {
  shouldApplyComposerCancellation,
  shouldClearComposerCancellation,
} from "./composer-cancel-outcome";

const acceptedPresent = {
  status: "cancelled",
  ack: "accepted",
  target_state: "present",
  durable: true,
} as const;

describe("shouldApplyComposerCancellation", () => {
  it("applies only an exact durable cancelled outcome", () => {
    expect(
      shouldApplyComposerCancellation(acceptedPresent),
    ).toBe(true);
    expect(
      shouldApplyComposerCancellation({ ...acceptedPresent, durable: false }),
    ).toBe(false);
  });

  it("keeps SSE open when a late Stop loses to a durable terminal Turn", () => {
    const noop = {
      ...acceptedPresent,
      status: "noop",
      ack: "rejected",
    } as const;
    expect(shouldApplyComposerCancellation(noop)).toBe(false);
    expect(shouldClearComposerCancellation(noop)).toBe(true);
  });

  it("clears an accepted receipt whose target Turn has been erased", () => {
    const erased = { ...acceptedPresent, target_state: "erased" } as const;
    expect(shouldApplyComposerCancellation(erased)).toBe(false);
    expect(shouldClearComposerCancellation(erased)).toBe(true);
  });
});

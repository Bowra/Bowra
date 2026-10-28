import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerRecoveryNotice } from "./composer-recovery-notice";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

afterEach(cleanup);

describe("ComposerRecoveryNotice", () => {
  it("shows the unresolved state and both decisions without a hidden menu", () => {
    render(
      <ComposerRecoveryNotice
        onRetry={async () => true}
        onAbandon={async () => true}
      />,
    );

    expect(screen.getByRole("region", { name: "submission_recovery_title" })).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain("submission_recovered");
    expect(screen.getByRole("button", { name: "submission_retry" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "submission_abandon" })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("returns focus to the reference editor after a successful resolution", async () => {
    const onRetry = vi.fn(async () => true);

    function Harness() {
      const [pending, setPending] = useState(true);
      return (
        <div data-composer-layout-v2>
          <div contentEditable role="combobox" aria-label="composer" />
          {pending ? (
            <ComposerRecoveryNotice
              onRetry={async () => {
                const accepted = await onRetry();
                if (accepted) setPending(false);
                return accepted;
              }}
              onAbandon={async () => false}
            />
          ) : null}
        </div>
      );
    }

    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "submission_retry" }));

    await waitFor(() => expect(onRetry).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(document.activeElement).toBe(
      screen.getByRole("combobox", { name: "composer" }),
    ));
  });

  it("preserves the actions and announces a failed recovery", async () => {
    render(
      <ComposerRecoveryNotice
        onRetry={async () => false}
        onAbandon={async () => false}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "submission_retry" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "submission_recovery_failed",
    );
    expect(screen.getByRole("button", { name: "submission_retry" })).toBeTruthy();
  });

  it("labels terminal recovery as lookup-only and hides an unavailable resend", () => {
    render(
      <ComposerRecoveryNotice
        variant="terminalAction"
        allowAbandon={false}
        onRetry={async () => false}
        onAbandon={async () => false}
      />,
    );
    expect(screen.getByRole("status").textContent).toContain(
      "action_recovery_pending",
    );
    expect(screen.getByRole("button", { name: "action_recovery_check" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "action_recovery_resend" })).toBeNull();
  });
});

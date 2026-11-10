import "@testing-library/jest-dom/vitest";
import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BowraCallback } from "./bowra-callback";
import { completeBowraConnection } from "@/lib/bowra-connect-api";
import { ApiError } from "@/lib/api-client";
import messages from "@/messages/en-US.json";

vi.mock("@/lib/bowra-connect-api", () => ({ completeBowraConnection: vi.fn() }));
vi.mock("@/components/public-shell", () => ({ PublicShell: ({ children }: { children: React.ReactNode }) => <main>{children}</main> }));

function mount(query: string) {
  window.history.replaceState(null, "", `/connect/bowra/callback/?${query}`);
  return render(
    <StrictMode>
      <NextIntlClientProvider locale="en-US" messages={messages}>
        <BowraCallback />
      </NextIntlClientProvider>
    </StrictMode>,
  );
}

beforeEach(() => {
  vi.mocked(completeBowraConnection)
    .mockReset()
    .mockImplementation(() => new Promise(() => {}));
});

describe("Bowra browser handoff", () => {
  it("removes the authorization query before posting and submits once in Strict Mode", () => {
    vi.mocked(completeBowraConnection).mockImplementation(() => {
      expect(window.location.search).toBe("");
      return new Promise(() => {});
    });
    mount("state=one-use&code=private-code");
    expect(completeBowraConnection).toHaveBeenCalledExactlyOnceWith({ state: "one-use", code: "private-code" });
    expect(screen.getByRole("status")).toHaveTextContent("Receiving Bowra configuration");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("passes a denial to the server for state validation and consumption", () => {
    mount("state=one-use&error=access_denied");
    expect(completeBowraConnection).toHaveBeenCalledExactlyOnceWith({ state: "one-use", error: "access_denied" });
  });

  it.each([
    "code=a",
    "state=&code=a",
    "state=a&state=b&code=a",
    "state=a&code=a&code=b",
    "state=a&error=a&error=b",
    "state=a&code=a&error=access_denied",
    "state=a",
    "state=a&code=",
    "state=a&error=",
  ])("rejects malformed callback %s before exchange", (query) => {
    mount(query);
    expect(completeBowraConnection).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(messages.settings.models.bowra.invalid_callback);
    expect(window.location.search).toBe("");
  });

  it.each([401, 403, 503])("keeps an HTTP %i failure on the clean page without retrying", async (status) => {
    vi.mocked(completeBowraConnection).mockRejectedValue(new ApiError(status, "failed"));
    mount("state=one-use&code=private-code");
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        status === 503 ? messages.settings.models.bowra.exchange_uncertain : messages.settings.models.bowra.permission_changed,
      ),
    );
    expect(completeBowraConnection).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("link", { name: "Return to Models" })).toHaveAttribute("href", "/team/settings/models/");
    expect(window.location.search).toBe("");
  });
});

import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { AuthShell } from "./auth-shell";

afterEach(cleanup);

describe("AuthShell language access", () => {
  it("keeps the language switcher visible on signed-out pages", () => {
    renderIntl(
      <AuthShell title="Sign in">
        <div>Form</div>
      </AuthShell>,
      { locale: "en-US" },
    );

    expect(
      screen.getByRole("button", {
        name: "Interface language: English",
      }),
    ).toBeTruthy();
  });
});

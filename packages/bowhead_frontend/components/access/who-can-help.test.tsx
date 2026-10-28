import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { AdminRouteState } from "@/components/shell/AdminRouteState";

const session = vi.hoisted(() => ({ capabilities: {} as Record<string, unknown> }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => ({ me: { capabilities: session.capabilities } }) }));

import { AccessWhoCanHelp, AccessWhoCanHelpWhenEnabled } from "./access-request-panel";

afterEach(() => {
  cleanup();
  session.capabilities = {};
});

describe("who can help on role pages", () => {
  it("admin console forbidden state: off renders the legacy card unchanged", () => {
    const { container: withSlot } = renderIntl(
      <AdminRouteState title="Audit" tone="forbidden" aside={<AccessWhoCanHelpWhenEnabled scope="team" />}>
        Not allowed
      </AdminRouteState>,
    );
    const slotted = withSlot.innerHTML;
    cleanup();
    const { container: legacy } = renderIntl(
      <AdminRouteState title="Audit" tone="forbidden">
        Not allowed
      </AdminRouteState>,
    );
    expect(slotted).toBe(legacy.innerHTML);
  });

  it("admin console forbidden state: on explains that the team owner can change the role, with no request button", () => {
    session.capabilities = { access_requests: true };
    renderIntl(
      <AdminRouteState title="Audit" tone="forbidden" aside={<AccessWhoCanHelpWhenEnabled scope="team" />}>
        Not allowed
      </AdminRouteState>,
    );
    expect(screen.getByTestId("access-who-can-help-team").textContent).toContain("团队负责人可以调整你的团队角色");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("space scope names the space admins", () => {
    renderIntl(<AccessWhoCanHelp scope="space" />);
    expect(screen.getByText("谁能帮你")).toBeTruthy();
    expect(screen.getByText(/这个空间的管理员可以调整你在空间里的角色/)).toBeTruthy();
  });
});

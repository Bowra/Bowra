import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { TransferOwnerDialog } from "./transfer-owner-dialog";
import * as api from "@/lib/admin-members-api";

const member = {
  user_id: "u_target",
  email: "target@example.com",
  display_name: "Target",
  tier: "pro",
  is_owner: false,
  team_role: "member" as const,
  promoted_at: null,
  created_at: "2026-05-01T00:00:00Z",
  last_active_at: null,
};

describe("TransferOwnerDialog", () => {
  beforeEach(() => {
    vi.spyOn(api, "transferTeamOwnership").mockResolvedValue({ ok: true, owner_user_id: member.user_id });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("confirms the target and transfers ownership", async () => {
    const onSuccess = vi.fn();
    renderIntl(<TransferOwnerDialog member={member} onClose={() => {}} onSuccess={onSuccess} />);

    expect(screen.getByText(/确认将团队负责人转让给 Target/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认转让" }));

    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
    expect(api.transferTeamOwnership).toHaveBeenCalledWith("u_target");
  });

  it("shows a localized safe error", async () => {
    vi.mocked(api.transferTeamOwnership).mockRejectedValueOnce(
      new api.AdminMembersError(422, "owner_transfer_target_not_active", "owner_transfer_target_not_active"),
    );
    renderIntl(<TransferOwnerDialog member={member} onClose={() => {}} onSuccess={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "确认转让" }));
    await waitFor(() => expect(screen.getByText(/该成员已不在当前团队中/)).toBeTruthy());
    expect(screen.queryByText("owner_transfer_target_not_active")).toBeNull();
  });
});

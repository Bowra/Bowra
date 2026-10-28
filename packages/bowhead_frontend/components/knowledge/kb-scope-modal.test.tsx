import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import messagesZh from "@/messages/zh-CN.json";

vi.mock("@/lib/kb-api", () => ({ bulkChangeKbFilesScope: vi.fn() }));

import { KbScopeModal } from "./kb-scope-modal";

function renderModal(isTeamAdmin: boolean) {
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <KbScopeModal
        targets={[{ file_id: "kbf_1", filename: "a.pdf", scope: "personal" }]}
        bulk={false}
        me={{ user_id: "u_1", team_id: "t_1", is_team_admin: isTeamAdmin, workspace_id: "ws_1" }}
        onClose={vi.fn()}
        onMigrated={vi.fn()}
        toast={vi.fn()}
      />
    </NextIntlClientProvider>,
  );
  return (sc: string) => screen.getByTestId(`kb-scope-modal-${sc}`);
}

afterEach(cleanup);

describe("KbScopeModal · 范围单选", () => {
  it("卡片是单选组：没有选中时 Tab 停在第一项，方向键与 Home/End 移动并选中", () => {
    const card = renderModal(true);
    expect(screen.getByRole("radiogroup", { name: "更改可见范围" })).toBeTruthy();
    expect(["personal", "space", "team"].map((sc) => card(sc).tabIndex)).toEqual([0, -1, -1]);
    expect(card("personal").getAttribute("aria-checked")).toBe("false");

    card("personal").focus();
    fireEvent.keyDown(card("personal"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(card("space"));
    expect(card("space").getAttribute("aria-checked")).toBe("true");
    expect(card("space").className).toContain("on");
    expect((screen.getByRole("button", { name: messagesZh.knowledge_base.scope_modal.next }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.keyDown(card("space"), { key: "End" });
    expect(card("team").getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(card("team"), { key: "Home" });
    expect(document.activeElement).toBe(card("personal"));
    expect(card("personal").getAttribute("aria-checked")).toBe("true");
    expect(["personal", "space", "team"].map((sc) => card(sc).tabIndex)).toEqual([0, -1, -1]);
    // Selecting the original scope again disables Next
    expect((screen.getByRole("button", { name: messagesZh.knowledge_base.scope_modal.next }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("非团队管理员：团队卡片禁用，方向键与 End 跳过它", () => {
    const card = renderModal(false);
    expect((card("team") as HTMLButtonElement).disabled).toBe(true);
    card("personal").focus();
    fireEvent.keyDown(card("personal"), { key: "End" });
    expect(document.activeElement).toBe(card("space"));
    expect(card("space").getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(card("space"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(card("personal"));
    expect(card("team").getAttribute("aria-checked")).toBe("false");
  });

  it("点击卡片选中", () => {
    const card = renderModal(true);
    fireEvent.click(card("team"));
    expect(card("team").getAttribute("aria-checked")).toBe("true");
    expect(card("team").tabIndex).toBe(0);
  });
});

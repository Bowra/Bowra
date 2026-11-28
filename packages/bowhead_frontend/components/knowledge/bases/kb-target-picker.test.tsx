// Knowledge bases (FEATURE_KNOWLEDGE_BASES): the "Add to" / move target picker (PRD §4.6, §4.7).
import "@testing-library/jest-dom/vitest";

import { useState } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { KnowledgeBaseDirectory } from "@/lib/knowledge-bases-api";
import messagesZh from "@/messages/zh-CN.json";

import { SPACES_FIXTURE, directoryFixture } from "./kb-target-picker.fixtures";

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ items: SPACES_FIXTURE, activeId: "ws_1", loading: false, error: null, setActiveId: () => {}, refresh: () => {} }),
}));

import { KnowledgeBaseTargetPicker, defaultKnowledgeBaseTarget, knowledgeBaseUploadScope } from "./kb-target-picker";

afterEach(cleanup);

function Harness({
  directory = directoryFixture(),
  initial = null,
  purpose = "add",
  excludeId = null,
  loading = false,
  onChange,
}: {
  directory?: KnowledgeBaseDirectory | null;
  initial?: string | null;
  purpose?: "add" | "move";
  excludeId?: string | null;
  loading?: boolean;
  onChange?: (id: string) => void;
}) {
  const [value, setValue] = useState<string | null>(initial);
  return (
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <span id="picker-label">添加到</span>
      <KnowledgeBaseTargetPicker
        directory={directory}
        loading={loading}
        value={value}
        onChange={(id) => { onChange?.(id); setValue(id); }}
        purpose={purpose}
        excludeId={excludeId}
        labelledBy="picker-label"
      />
    </NextIntlClientProvider>
  );
}

const radio = (id: string) => screen.getByTestId(`kb-target-${id}`);

describe("KnowledgeBaseTargetPicker", () => {
  it("groups My, the current Space by name and Team, with default names, the Default badge and each audience", () => {
    render(<Harness />);
    const group = screen.getByRole("radiogroup", { name: "添加到" });
    expect(within(group).getByRole("group", { name: "我的" })).toBeInTheDocument();
    expect(within(group).getByRole("group", { name: "商超零售场景" })).toBeInTheDocument();
    expect(within(group).getByRole("group", { name: "团队" })).toBeInTheDocument();
    expect(radio("kb_my")).toHaveTextContent("我的知识");
    expect(radio("kb_my")).toHaveTextContent("默认");
    expect(radio("kb_my")).toHaveTextContent("仅自己");
    // A Space default is named after its Space.
    expect(radio("kb_space")).toHaveTextContent("商超零售场景");
    expect(radio("kb_space")).toHaveTextContent("空间成员可编辑");
    expect(radio("kb_ops")).toHaveTextContent("门店运营手册");
    expect(radio("kb_ops")).not.toHaveTextContent("默认");
    expect(radio("kb_team")).toHaveTextContent("团队知识库");
    expect(radio("kb_policy")).toHaveTextContent("团队所有人可使用");
  });

  it("keeps knowledge bases you cannot add to visible, disabled, with whom to ask", () => {
    render(<Harness />);
    expect(radio("kb_contracts")).toBeDisabled();
    expect(radio("kb_contracts")).toHaveTextContent("你只能使用这个知识库。需要添加文件时，请联系所有者王蕾。");
    // Defaults have no owner: their home's managers are named instead.
    expect(radio("kb_team")).toBeDisabled();
    expect(radio("kb_team")).toHaveTextContent("请联系团队负责人或团队管理员");
    expect(radio("kb_ops")).toBeEnabled();
  });

  it("selects on click and with the arrows, skipping disabled options; Home and End reach the edges", () => {
    const onChange = vi.fn();
    render(<Harness initial="kb_space" onChange={onChange} />);
    expect(radio("kb_space")).toHaveAttribute("aria-checked", "true");
    expect(radio("kb_space")).toHaveAttribute("tabindex", "0");
    expect(radio("kb_ops")).toHaveAttribute("tabindex", "-1");
    fireEvent.click(radio("kb_ops"));
    expect(onChange).toHaveBeenLastCalledWith("kb_ops");
    radio("kb_ops").focus();
    // The next options (供应商合同, 团队知识库, 公司制度) are disabled: the arrow wraps to the first enabled one.
    fireEvent.keyDown(radio("kb_ops"), { key: "ArrowDown" });
    expect(onChange).toHaveBeenLastCalledWith("kb_my");
    expect(radio("kb_my")).toHaveAttribute("aria-checked", "true");
    fireEvent.keyDown(radio("kb_my"), { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith("kb_ops");
    fireEvent.keyDown(radio("kb_ops"), { key: "Home" });
    expect(onChange).toHaveBeenLastCalledWith("kb_my");
    expect(onChange).not.toHaveBeenCalledWith("kb_contracts");
  });

  it("move mode lists only knowledge bases you can edit and marks the current one", () => {
    render(<Harness purpose="move" excludeId="kb_ops" />);
    expect(screen.queryByTestId("kb-target-kb_contracts")).toBeNull();
    expect(screen.queryByTestId("kb-target-kb_team")).toBeNull();
    expect(screen.queryByRole("group", { name: "团队" })).toBeNull();
    expect(radio("kb_ops")).toBeDisabled();
    expect(radio("kb_ops")).toHaveTextContent("当前");
    expect(radio("kb_ops")).toHaveTextContent("所选文件现在就在这个知识库里。");
    expect(radio("kb_space")).toBeEnabled();
  });

  it("shows a skeleton while the directory loads and an empty state with nothing to choose", () => {
    const { unmount } = render(<Harness directory={null} loading />);
    expect(screen.getByRole("status", { name: messagesZh.knowledge_bases.common.loading })).toBeInTheDocument();
    unmount();
    const onlyCurrent: KnowledgeBaseDirectory = { ...directoryFixture(), sections: { personal: [], space: [directoryFixture().sections.space[1]], team: [] } };
    render(<Harness directory={onlyCurrent} purpose="move" excludeId="kb_ops" />);
    expect(screen.getByTestId("kb-target-empty")).toHaveTextContent("没有其他可以移入的知识库");
  });
});

describe("defaultKnowledgeBaseTarget", () => {
  it("prefers the given knowledge base when you can add to it", () => {
    expect(defaultKnowledgeBaseTarget(directoryFixture(), "kb_ops")).toBe("kb_ops");
  });

  it("falls back to the current Space's default, then My knowledge", () => {
    expect(defaultKnowledgeBaseTarget(directoryFixture(), "kb_contracts")).toBe("kb_space");
    expect(defaultKnowledgeBaseTarget(directoryFixture(), null)).toBe("kb_space");
    expect(defaultKnowledgeBaseTarget(directoryFixture({ spaceDefaultEditable: false }), "kb_policy")).toBe("kb_my");
    expect(defaultKnowledgeBaseTarget(null)).toBeNull();
  });

  it("maps a target's home to the upload scope", () => {
    const { personal, space, team } = directoryFixture().sections;
    expect(knowledgeBaseUploadScope(personal[0])).toEqual({ scope: "personal", scope_id: "u_1" });
    expect(knowledgeBaseUploadScope(space[1])).toEqual({ scope: "space", scope_id: "ws_1" });
    expect(knowledgeBaseUploadScope(team[0])).toEqual({ scope: "team", scope_id: "team_1" });
  });
});

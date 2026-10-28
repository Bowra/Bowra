import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { ChatCard, ChatCardButton } from "./card";

const copyDiagnosticsMock = vi.hoisted(() => vi.fn(async () => true));
vi.mock("@/components/error-notice", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  copyDiagnostics: copyDiagnosticsMock,
}));

describe("ChatCard shell", () => {
  it("is a group named by its status word and title", () => {
    renderIntl(<ChatCard intent="input" eyebrow="需要你回答" title="按哪种口径比较？" />);
    const card = screen.getByRole("group", { name: "需要你回答 按哪种口径比较？" });
    expect(card.className).toContain("ui-card--input");
    expect(card.className).toContain("ui-card--regular");
  });

  it("uses the default icon of each intent and a spinner while in progress", () => {
    const { container, rerender } = renderIntl(<ChatCard intent="failure" title="t" />);
    expect(container.querySelector(".ui-card__icon svg")?.getAttribute("class")).toContain("lucide-circle-alert");
    rerender(<ChatCard intent="success" size="row" title="t" />);
    expect(container.querySelector(".ui-card__icon svg")?.getAttribute("class")).toContain("lucide-check");
    rerender(<ChatCard intent="progress" title="t" />);
    expect(container.querySelector(".ui-card__icon .ui-card__spinner")).not.toBeNull();
    expect(container.querySelector(".ui-card__icon svg")).toBeNull();
  });

  it("announces receipts politely, failures assertively, input cards and replays not at all", () => {
    const { container, rerender } = renderIntl(<ChatCard intent="success" size="row" title="已记住" />);
    expect(container.querySelector(".ui-card")?.getAttribute("aria-live")).toBe("polite");
    rerender(<ChatCard intent="failure" eyebrow="本轮没有完成" title="模型服务暂时没有响应" />);
    expect(screen.getByRole("alert").textContent).toBe("模型服务暂时没有响应");
    expect(container.querySelector(".ui-card")?.getAttribute("aria-live")).toBeNull();
    rerender(<ChatCard intent="failure" eyebrow="本轮没有完成" title="模型服务暂时没有响应" live="off" />);
    expect(screen.queryByRole("alert")).toBeNull();
    rerender(<ChatCard intent="input" title="要我以后都这样做吗？" />);
    expect(container.querySelector("[aria-live]")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps progress meta such as elapsed time out of announcements", () => {
    const { container } = renderIntl(<ChatCard intent="progress" eyebrow="正在生成" meta="38 秒" title="草稿" />);
    expect(container.querySelector(".ui-card__meta")?.getAttribute("aria-live")).toBe("off");
  });

  it("renders one primary action on the right and secondary actions on the left", () => {
    const onPrimary = vi.fn();
    const onSecondary = vi.fn();
    renderIntl(
      <ChatCard
        intent="input"
        title="t"
        primary={{ label: "继续", onSelect: onPrimary }}
        secondary={[{ label: "跳过", onSelect: onSecondary }]}
      />,
    );
    const buttons = within(screen.getByRole("group")).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["跳过", "继续"]);
    expect(buttons.filter((b) => b.className.includes("settings-button--primary"))).toHaveLength(1);
    expect(buttons[0].className).toContain("ui-card__btn--plain");
    fireEvent.click(buttons[1]);
    expect(onPrimary).toHaveBeenCalledTimes(1);
    fireEvent.click(buttons[0]);
    expect(onSecondary).toHaveBeenCalledTimes(1);
  });

  it("writes the reason an action is unavailable and links it to the button", () => {
    const onPrimary = vi.fn();
    renderIntl(
      <ChatCard
        intent="failure"
        title="你本期的额度已用完"
        primary={{ label: "重试", onSelect: onPrimary, disabledReason: "补充额度后可以重试" }}
      />,
    );
    const button = screen.getByRole("button", { name: "重试" });
    expect(button.getAttribute("aria-disabled")).toBe("true");
    const reasonId = button.getAttribute("aria-describedby");
    expect(reasonId && document.getElementById(reasonId)?.textContent).toBe("补充额度后可以重试");
    fireEvent.click(button);
    expect(onPrimary).not.toHaveBeenCalled();
  });

  it("spins the primary action in place and locks the other controls while it is busy", () => {
    renderIntl(
      <ChatCard
        intent="input"
        title="t"
        primary={{ label: "继续", busyLabel: "正在提交…", busy: true, onSelect: vi.fn() }}
        secondary={[{ label: "跳过", onSelect: vi.fn() }]}
      />,
    );
    const primary = screen.getByRole("button", { name: "正在提交…" });
    expect(primary.getAttribute("aria-busy")).toBe("true");
    expect((screen.getByRole("button", { name: "跳过" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("group").getAttribute("aria-busy")).toBe("true");
  });

  it("shows an inline error inside the card instead of a toast", () => {
    renderIntl(<ChatCard intent="input" title="t" error={{ message: "没能提交回答，你的选择已保留。" }} />);
    expect(screen.getByRole("alert").textContent).toContain("没能提交回答");
  });

  it("collapses technical details and never shows reason codes or turn ids, only the copy action", () => {
    renderIntl(
      <ChatCard
        intent="failure"
        title="回答里的数字和查到的数据对不上"
        details={{
          diagnostics: {
            schemaVersion: 1,
            disclosure: "authenticated",
            code: "document_evidence_mismatch",
            turnId: "t_secret_turn",
            observedAt: "2026-10-05T06:23:05.000Z",
          },
          location: "对话",
        }}
      />,
    );
    const toggle = screen.getByRole("button", { name: "技术详情" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("复制诊断信息")).toBeNull();
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const panel = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(panel?.textContent).toContain("对话");
    // b5a814577 renamed the label from 构建版本 to 界面版本.
    expect(panel?.textContent).toContain("界面版本");
    expect(panel?.textContent).not.toContain("document_evidence_mismatch");
    expect(panel?.textContent).not.toContain("t_secret_turn");
    fireEvent.click(screen.getByRole("button", { name: "复制诊断信息" }));
    expect(copyDiagnosticsMock).toHaveBeenCalledTimes(1);
  });
});

describe("ChatCard rows and activation", () => {
  it("renders a row as icon + sentence + trailing actions", () => {
    renderIntl(
      <ChatCard
        intent="success"
        size="row"
        title={<><b>已记住</b>：汇报 GMV 时同时给环比与同比</>}
        trailing={<ChatCardButton>撤销</ChatCardButton>}
      />,
    );
    const card = screen.getByRole("group", { name: "已记住：汇报 GMV 时同时给环比与同比" });
    expect(card.className).toContain("ui-card--row");
    expect(within(card).getByRole("button", { name: "撤销" })).toBeTruthy();
  });

  it("makes a row with onActivate one keyboard-reachable button", () => {
    const onActivate = vi.fn();
    renderIntl(
      <ChatCard
        intent="info"
        size="row"
        title="5 步完成"
        meta=" · 1 分 42 秒"
        trailing={<span className="ui-card__trailing-label">展开运行过程</span>}
        onActivate={onActivate}
        activateLabel="展开运行过程"
      />,
    );
    const button = screen.getByRole("button", { name: "展开运行过程" });
    expect(button.className).toContain("ui-card");
    expect(button.querySelector("button")).toBeNull();
    button.focus();
    expect(document.activeElement).toBe(button);
    fireEvent.click(button);
    expect(onActivate).toHaveBeenCalledTimes(1);
  });

  it("keeps the activation area and the trailing control as sibling buttons", () => {
    const onActivate = vi.fn();
    const onDownload = vi.fn();
    renderIntl(
      <ChatCard
        intent="info"
        media={<span>icon</span>}
        title="Q2 运营复盘.pptx"
        onActivate={onActivate}
        activateLabel="在成果区打开 Q2 运营复盘.pptx"
        trailing={<button type="button" onClick={onDownload}>下载</button>}
      />,
    );
    const open = screen.getByRole("button", { name: "在成果区打开 Q2 运营复盘.pptx" });
    const download = screen.getByRole("button", { name: "下载" });
    expect(open.contains(download)).toBe(false);
    expect(download.contains(open)).toBe(false);
    fireEvent.click(download);
    expect(onActivate).not.toHaveBeenCalled();
    fireEvent.click(open);
    expect(onActivate).toHaveBeenCalledTimes(1);
  });

  it("keeps error, footer actions and the disabled reason of an activatable card, outside its button", () => {
    const onActivate = vi.fn();
    const onRetry = vi.fn();
    renderIntl(
      <ChatCard
        intent="info"
        title="Q2 运营复盘.pptx"
        onActivate={onActivate}
        activateLabel="打开 Q2 运营复盘.pptx"
        error={{ message: "下载失败" }}
        secondary={[{ label: "重试", onSelect: onRetry }]}
        primary={{ label: "继续", onSelect: vi.fn(), disabledReason: "正在保存" }}
      />,
    );
    const open = screen.getByRole("button", { name: "打开 Q2 运营复盘.pptx" });
    const retry = screen.getByRole("button", { name: "重试" });
    expect(screen.getByRole("alert").textContent).toContain("下载失败");
    expect(screen.getByText("正在保存")).toBeTruthy();
    expect(open.contains(retry)).toBe(false);
    expect(open.contains(screen.getByRole("alert"))).toBe(false);
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onActivate).not.toHaveBeenCalled();
  });

  it("draws progress as a custom property for the bottom bar", () => {
    const { container } = renderIntl(<ChatCard intent="progress" title="t" progress={0.462} />);
    const card = container.querySelector<HTMLElement>(".ui-card");
    expect(card?.style.getPropertyValue("--card-progress")).toBe("46%");
    expect(container.querySelector(".ui-card__progress")).not.toBeNull();
  });

  it("renders ChatCardButton with href as a link", () => {
    renderIntl(<ChatCardButton href="/settings/memory/" variant="ghost">在记忆中查看</ChatCardButton>);
    const link = screen.getByRole("link", { name: "在记忆中查看" });
    expect(link.getAttribute("href")).toBe("/settings/memory/");
  });
});

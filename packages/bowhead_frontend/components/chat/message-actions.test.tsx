/**
 * chat-ux-2026-08 batch F — message-level action bar.
 *
 * Observed on 116: after a turn answered, the entire message had **no action entry**. Pin these rules:
 *   ① Show「复制」only with body text: BI/report answers live in canvas and chat runner.answer is empty;
 *      a button copying an empty string is worse than none;
 *   ② Clipboard failures must **not falsely claim**「已复制」;
 *   ③ Resend uses the real mechanism (onSend with the original question), never a dead button;
 *   ④ No「有帮助/没帮助」: there is no feedback endpoint in this repository,
 *      and TurnContainer's comments explicitly record the lesson from removing dead buttons last time.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "@/messages/zh-CN.json";

import { MessageActions, type MessageActionsProps } from "./message-actions";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mount(props: MessageActionsProps) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <MessageActions {...props} />
    </NextIntlClientProvider>,
  );
}

/** jsdom has no navigator.clipboard; install a controllable stub. */
function stubClipboard(impl: () => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: vi.fn(impl) },
    configurable: true,
  });
}

describe("MessageActions — 出现条件", () => {
  it("★ 答案为空时不出「复制」（BI/报告轮：答案在 canvas，chat 侧无正文）", () => {
    mount({ answerText: "", resendText: "近30天营业额", onResend: () => {} });
    expect(screen.queryByTestId("msg-action-copy")).toBeNull();
    expect(screen.getByTestId("msg-action-resend")).toBeTruthy();
  });

  it("只有空白字符也算没有正文", () => {
    mount({ answerText: "   \n  ", resendText: "问题", onResend: () => {} });
    expect(screen.queryByTestId("msg-action-copy")).toBeNull();
  });

  it("★ 没有 onSend 时不出「重新发送」（不做点了没去处的死按钮）", () => {
    mount({ answerText: "结论是 9.1 亿" });
    expect(screen.getByTestId("msg-action-copy")).toBeTruthy();
    expect(screen.queryByTestId("msg-action-resend")).toBeNull();
  });

  it("原问题为空时不出「重新发送」（空消息会被 chat/send 400 拒）", () => {
    mount({ answerText: "结论", resendText: "", onResend: () => {} });
    expect(screen.queryByTestId("msg-action-resend")).toBeNull();
  });

  it("★ 两个都没有 → 整条不渲染（不留空操作条）", () => {
    mount({ answerText: "", resendText: "" });
    expect(screen.queryByTestId("message-actions")).toBeNull();
  });

  it("没有「有帮助 / 没帮助」——后端无 feedback endpoint，不装死按钮", () => {
    mount({ answerText: "结论", resendText: "问题", onResend: () => {} });
    const labels = screen.getAllByRole("button").map((b) => b.textContent ?? "");
    expect(labels.some((l) => /帮助|👍|👎/.test(l))).toBe(false);
  });
});

describe("MessageActions — 复制", () => {
  it("复制答案原文并给出「已复制」回执", async () => {
    const writes: string[] = [];
    stubClipboard(async function (this: unknown, ...a: unknown[]) {
      writes.push(a[0] as string);
    } as () => Promise<void>);
    mount({ answerText: "  营业额约 9.10 亿  " });

    fireEvent.click(screen.getByTestId("msg-action-copy"));
    await waitFor(() => expect(writes).toEqual(["营业额约 9.10 亿"]));
    expect(screen.getByTestId("msg-action-copy").textContent).toContain("已复制");
  });

  it("★ 复制的文本不带 [figure:] 锚行（渲染指令≠给人看的正文）", async () => {
    const writes: string[] = [];
    stubClipboard(async function (this: unknown, ...a: unknown[]) {
      writes.push(a[0] as string);
    } as () => Promise<void>);
    mount({ answerText: "结论一。\n[figure:sec_a]\n**图1**：说明\n\n结论二。" });

    fireEvent.click(screen.getByTestId("msg-action-copy"));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).not.toContain("[figure:");
    expect(writes[0]).not.toContain("sec_a");
    expect(writes[0]).toContain("结论一。");
    expect(writes[0]).toContain("图1");
    expect(writes[0]).toContain("结论二。");
  });

  it("★ 剪贴板失败时不谎报「已复制」，退回 prompt 让用户手抄", async () => {
    stubClipboard(async () => {
      throw new Error("NotAllowedError");
    });
    const prompt = vi.spyOn(window, "prompt").mockReturnValue(null);
    mount({ answerText: "结论文本" });

    fireEvent.click(screen.getByTestId("msg-action-copy"));
    await waitFor(() => expect(prompt).toHaveBeenCalledWith(expect.any(String), "结论文本"));
    expect(screen.getByTestId("msg-action-copy").textContent).not.toContain("已复制");
  });
});

describe("MessageActions — 露出规则（CSS 契约）", () => {
  // jsdom cannot measure these three rules (no layout or media queries), but they are **source facts**,
  // and precisely the parts most likely to be casually cleaned away later.
  const css = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), "..", "..", "app", "globals.css"),
    "utf8",
  );
  const hoverBlock =
    /@media \(hover: hover\) and \(pointer: fine\) \{([\s\S]*?)\n\}/.exec(css)?.[1] ?? "";

  it("★ 隐藏规则**只**在 hover 媒体查询里 —— 触屏因此常驻（长按会和选中文字打架）", () => {
    expect(hoverBlock).toContain(".bh-msg-actions");
    expect(hoverBlock).toMatch(/\.bh-msg-actions\s*\{[^}]*opacity:\s*0/);
    // No declaration **outside** the media query may hide it.
    const outside = css.replace(hoverBlock, "");
    expect(outside).not.toMatch(/\.bh-msg-actions\s*\{[^}]*opacity:\s*0/);
  });

  it("★ 用 opacity 而不是 display:none —— 否则按钮不可聚焦，键盘用户永远够不到", () => {
    expect(css).not.toMatch(/\.bh-msg-actions[^{]*\{[^}]*display:\s*none/);
  });

  it("键盘 focus 也能唤出（不能只认 :hover）", () => {
    expect(hoverBlock).toContain(".bh-msg-actions:focus-within");
  });
});

describe("MessageActions — 重新发送", () => {
  it("★ 回传的是原问题文本（与 FinalFailBanner.onPrimary 同一条重发路径）", async () => {
    const onResend = vi.fn();
    mount({ answerText: "结论", resendText: "  近30天 Top5  ", onResend });
    fireEvent.click(screen.getByTestId("msg-action-resend"));
    expect(onResend).toHaveBeenCalledWith("近30天 Top5");
  });

  it("★ 复制的文本不带引用与机器标记（角标脱离页面没有来源可点）", async () => {
    const writes: string[] = [];
    stubClipboard(async function (this: unknown, ...a: unknown[]) {
      writes.push(a[0] as string);
    } as () => Promise<void>);
    mount({ answerText: "官方指导价 6499 元起 [cite:1]，线上首发[cite:2]。总盘见 [data:art_abc123]；来源 [bi:订单表]" });
    fireEvent.click(screen.getByTestId("msg-action-copy"));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toBe("官方指导价 6499 元起，线上首发。总盘见；来源");
  });
});

describe("icon buttons, with the latest answer's actions always shown", () => {
  it("the latest answer's action bar carries data-latest (always shown on pointer devices); older ones do not", () => {
    const { container, unmount } = mount({ answerText: "答案", latest: true });
    expect(container.querySelector(".bh-msg-actions")?.getAttribute("data-latest")).toBe("1");
    unmount();
    const older = mount({ answerText: "答案" });
    expect(older.container.querySelector(".bh-msg-actions")?.hasAttribute("data-latest")).toBe(false);
  });

  it("copy has a tooltip name; after copying the button carries data-state=copied", async () => {
    stubClipboard(() => Promise.resolve());
    mount({ answerText: "答案" });
    const btn = screen.getByTestId("msg-action-copy");
    expect(btn.getAttribute("title")).toBe(zh.chat.msg_copy);
    expect(btn.hasAttribute("data-state")).toBe(false);
    fireEvent.click(btn);
    await waitFor(() => expect(btn.getAttribute("data-state")).toBe("copied"));
    expect(btn.hasAttribute("title")).toBe(false);
  });

  it("edit and resend uses the pencil icon with the same name and action", () => {
    const onResend = vi.fn();
    mount({ resendText: "原问题", onResend });
    const btn = screen.getByTestId("msg-action-resend");
    expect(btn.querySelector("svg.lucide-pencil")).not.toBeNull();
    expect(btn.getAttribute("aria-label")).toBe(zh.chat.msg_resend);
    fireEvent.click(btn);
    expect(onResend).toHaveBeenCalledWith("原问题");
  });
});

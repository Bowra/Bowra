import { readFileSync } from "node:fs";
import { join } from "node:path";

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type {
  TurnMemoryExplicitRequest,
  TurnMemoryReceiptEntry,
} from "@/lib/memory-api";

import { MemoryDisclose, NOT_REMEMBERED_REASONS } from "./memory-disclose";

const _entry = (
  over: Partial<TurnMemoryReceiptEntry> = {},
): TurnMemoryReceiptEntry => ({
  id: "mem_1",
  kind: "preference",
  key: "ranking_chart_type",
  display_value: "排名图用横向条形图",
  origin: "capture",
  state: "remembered",
  undoable: true,
  candidate_id: null,
  ...over,
});

const _request = (
  over: Partial<TurnMemoryExplicitRequest> = {},
): TurnMemoryExplicitRequest => ({
  source: "language_setting",
  outcome: "not_remembered",
  reason: "trigger_language_setting",
  ...over,
});

// This repo's vitest has no automatic cleanup: stale DOM from the prior case makes getByTestId report multiple matches.
afterEach(cleanup);

describe("零点击披露条（设计稿 B）", () => {
  it("★★ 记了什么、记在哪（个人偏好）、怎么撤 —— 三件事都得在", () => {
    // Capturing a preference on this path happens **without any user click**.
    // Zero-click + undo is justified only if users immediately know and can immediately undo.
    renderIntl(<MemoryDisclose entries={[_entry()]} undo={vi.fn()} />);
    const row = screen.getByTestId("memory-disclose-entry-mem_1");
    expect(row.textContent).toContain("已记为个人偏好：排名图用横向条形图");
    expect(row.className).toContain("ui-card--success");
    expect(screen.getByTestId("memory-disclose-undo-mem_1")).toBeTruthy();
  });

  it("没有条目也没有未记住的请求时连容器都不画 —— 空是常态不是错误", () => {
    renderIntl(<MemoryDisclose entries={[]} undo={vi.fn()} />);
    expect(screen.queryByTestId("memory-disclose")).toBeNull();
  });

  it("links to Settings › Memory with the 「在记忆中查看」 label", () => {
    renderIntl(<MemoryDisclose entries={[_entry()]} undo={vi.fn()} />);
    const link = screen.getByRole("link");
    expect(link.getAttribute("href")).toBe("/settings/memory/");
    expect(link.textContent).toBe("在记忆中查看");
  });

  it("撤销拿的是条目 id", async () => {
    const undo = vi.fn().mockResolvedValue(true);
    renderIntl(<MemoryDisclose entries={[_entry()]} undo={undo} />);
    fireEvent.click(screen.getByTestId("memory-disclose-undo-mem_1"));
    await waitFor(() => expect(undo).toHaveBeenCalledWith("mem_1"));
  });

  it("★★ an undone entry reads 「已撤销」 and offers no second Undo", () => {
    // The server derives `undone` from the trash, so this is also what a refresh renders.
    renderIntl(
      <MemoryDisclose
        entries={[_entry({ state: "undone", undoable: false })]}
        undo={vi.fn()}
      />,
    );
    expect(screen.getByText("已撤销")).toBeTruthy();
    expect(screen.queryByText("已记为个人偏好")).toBeNull();
    expect(screen.queryByTestId("memory-disclose-undo-mem_1")).toBeNull();
    expect(
      screen.getByTestId("memory-disclose-entry-mem_1").getAttribute("data-state"),
    ).toBe("undone");
  });

  it("a failed undo settles as an error row with Retry (no toast)", async () => {
    const undo = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    renderIntl(<MemoryDisclose entries={[_entry()]} undo={undo} />);
    fireEvent.click(screen.getByTestId("memory-disclose-undo-mem_1"));
    await waitFor(() =>
      expect(screen.getByTestId("memory-disclose-entry-mem_1").getAttribute("data-state")).toBe("undo_failed"),
    );
    const row = screen.getByTestId("memory-disclose-entry-mem_1");
    expect(row.textContent).toContain("没能撤销，这条仍在记忆里");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(undo).toHaveBeenCalledTimes(2));
  });

  it("a locked entry is shown without Undo", () => {
    renderIntl(
      <MemoryDisclose entries={[_entry({ undoable: false })]} undo={vi.fn()} />,
    );
    expect(screen.queryByTestId("memory-disclose-undo-mem_1")).toBeNull();
  });

  it("falls back to a generic line without a display value, never the internal key", () => {
    renderIntl(
      <MemoryDisclose
        entries={[_entry({ display_value: null })]}
        undo={vi.fn()}
      />,
    );
    const text = screen.getByTestId("memory-disclose-entry-mem_1").textContent;
    expect(text).toContain("已记住一条个人偏好");
    expect(text).not.toContain("ranking_chart_type");
  });
});

describe("explicit request that was not remembered", () => {
  it("★★ 没有任何 entry 也要能露面，并说出原因", () => {
    renderIntl(
      <MemoryDisclose entries={[]} explicitRequest={_request()} undo={vi.fn()} />,
    );
    expect(screen.getByTestId("memory-disclose-notice")).toBeTruthy();
    expect(screen.getByText("没有记住")).toBeTruthy();
    expect(screen.getByText(/回答语言由设置决定/)).toBeTruthy();
  });

  it("★ 不给「去设置里改」的入口 —— 那个控件今天不存在", () => {
    renderIntl(
      <MemoryDisclose entries={[]} explicitRequest={_request()} undo={vi.fn()} />,
    );
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("★ no 「没有记住」 when the sentence already landed as a card or entry", () => {
    renderIntl(
      <MemoryDisclose
        entries={[]}
        explicitRequest={_request({ source: "capture", reason: "draft_says_not_lasting" })}
        undo={vi.fn()}
        hasLandedMemory
      />,
    );
    expect(screen.queryByTestId("memory-disclose")).toBeNull();
    expect(screen.queryByText("没有记住")).toBeNull();
  });

  it("entry_locked has its own wording, not the editing-lock copy", () => {
    renderIntl(
      <MemoryDisclose
        entries={[]}
        explicitRequest={_request({ source: "remember_tool", reason: "entry_locked" })}
        undo={vi.fn()}
      />,
    );
    expect(screen.getByText(/被锁定的记忆/)).toBeTruthy();
    expect(screen.queryByText(/你正在编辑记忆/)).toBeNull();
  });

  it("a null or unknown reason renders only the generic chip", () => {
    for (const reason of [null, "suppressed"]) {
      renderIntl(
        <MemoryDisclose
          entries={[]}
          explicitRequest={_request({ source: "nl_intent", reason })}
          undo={vi.fn()}
        />,
      );
      const notice = screen.getByTestId("memory-disclose-notice");
      expect(notice.textContent).toBe("没有记住");
      cleanup();
    }
  });

  it("remembered and candidate outcomes add no notice (entries and cards already show them)", () => {
    for (const outcome of ["remembered", "candidate"] as const) {
      renderIntl(
        <MemoryDisclose
          entries={[]}
          explicitRequest={_request({ source: "remember_tool", outcome, reason: null })}
          undo={vi.fn()}
        />,
      );
      expect(screen.queryByTestId("memory-disclose")).toBeNull();
      cleanup();
    }
  });
});

describe("not_remembered_reason copy registry", () => {
  type Catalog = { memory_card: Record<string, unknown> };
  const catalogs: [string, Catalog][] = ["zh-CN", "en-US", "de-DE", "fr-FR"].map(
    (locale) => [
      locale,
      JSON.parse(
        readFileSync(
          join(__dirname, "../../edition/shared-messages", `${locale}.json`),
          "utf8",
        ),
      ) as Catalog,
    ],
  );

  it.each(catalogs)("%s has exactly one string per displayable reason", (_locale, catalog) => {
    const table = catalog.memory_card.not_remembered_reason as Record<string, string>;
    expect(Object.keys(table).sort()).toEqual([...NOT_REMEMBERED_REASONS].sort());
    for (const text of Object.values(table)) expect(text.trim()).not.toBe("");
  });

  it("links never point readers at a 「记忆页」 / Memory page", () => {
    for (const [, catalog] of catalogs) {
      const card = JSON.stringify(catalog.memory_card);
      expect(card).not.toMatch(/记忆页|Memory page|Gedächtnisseite|page Mémoire/);
    }
  });

  it("zh-CN reasons use full-width punctuation", () => {
    const zh = catalogs[0][1].memory_card.not_remembered_reason as Record<string, string>;
    for (const text of Object.values(zh)) expect(text).not.toMatch(/[,.;:!?]/);
  });
});

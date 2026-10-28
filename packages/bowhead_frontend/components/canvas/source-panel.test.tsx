import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { CanvasContextProvider } from "@/lib/canvas-pm-adapter/canvas-context";
import type { SourceRef } from "@/lib/canvas-pm-adapter/citation-types";
import { SourceAccessRegistryProvider } from "@/lib/source-access-registry";
import {
  HighlightedBlock,
  SourcePanel,
  SourcePanelProvider,
  findHighlightSpan,
  useSourcePanel,
} from "./source-panel";

const ALIAS = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("findHighlightSpan", () => {
  it("returns null for empty content or highlight", () => {
    expect(findHighlightSpan("", "x")).toBeNull();
    expect(findHighlightSpan("text", "")).toBeNull();
  });

  it("finds exact and normalized-whitespace substrings", () => {
    expect(findHighlightSpan("hello world", "world")).toEqual([6, 11]);
    expect(findHighlightSpan("营收\t32.4 亿元，\n  达历史新高。", "营收 32.4 亿元")).not.toBeNull();
  });

  it("returns null when no match", () => {
    expect(findHighlightSpan("apples and oranges", "bananas")).toBeNull();
  });
});

describe("HighlightedBlock", () => {
  it("renders a quote-only fallback when content is absent", () => {
    renderIntl(<HighlightedBlock content="" highlight="some sentence" />);
    expect(screen.getByText("some sentence")).toBeInTheDocument();
    expect(screen.getByText("只显示了引用的句子。")).toBeInTheDocument();
  });

  it("marks an exact match", () => {
    renderIntl(<HighlightedBlock content="Q3 营收 32.4 亿元" highlight="32.4 亿元" />);
    expect(document.querySelector("mark")?.textContent).toBe("32.4 亿元");
  });

  it("shows the mismatch notice without inventing a match", () => {
    renderIntl(<HighlightedBlock content="unrelated" highlight="missing" />);
    expect(screen.getByText(/未在来源产物文本中匹配/)).toBeInTheDocument();
  });
});

function mockMatchMedia(mobile = false) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockReturnValue({
      matches: mobile,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  });
}

function renderPanel(sourceRef: SourceRef) {
  function Opener() {
    const { open } = useSourcePanel();
    return (
      <button
        type="button"
        onClick={(event) => open({
          sourceRef,
          sourceQuality: sourceRef.access_state === "linked_accessible" ? "linked_accessible" : "unavailable",
          sourceIndex: 0,
          display: "[1]",
          value: "",
          unit: "",
          highlight: "",
        }, event.currentTarget)}
      >
        open
      </button>
    );
  }

  return renderIntl(
    <SourceAccessRegistryProvider>
      <CanvasContextProvider sections={[]} sourceRefs={[]}>
        <SourcePanelProvider>
          <Opener />
          <SourcePanel />
        </SourcePanelProvider>
      </CanvasContextProvider>
    </SourceAccessRegistryProvider>,
  );
}

describe("SourcePanel current-viewer access", () => {
  it("hides detail while checking, then atomically renders safe detail", async () => {
    mockMatchMedia();
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        family: "table",
        mode: "rows",
        columns: ["region", "revenue"],
        rows: [{ region: "East", revenue: 42 }],
        total_rows: 1,
      }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderPanel({
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_accessible",
      generic_type: "query_result",
      ordinal: 1,
      safe_label: "Revenue source",
      safe_query: "Revenue by region",
      safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
      safe_preview_columns: ["region", "revenue"],
      safe_preview_rows: [{ region: "West", revenue: "41" }],
      safe_total_rows: 1,
    });

    fireEvent.click(screen.getByRole("button", { name: "open" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/正在重新确认/)).toBeInTheDocument();
    expect(within(dialog).queryByText("Revenue source")).toBeNull();

    await waitFor(() => expect(within(dialog).getByText("Revenue source")).toBeInTheDocument());
    expect(within(dialog).getByText(/West/)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(String(fetchSpy.mock.calls[0]?.[0])).toContain("/api/v1/report-artifacts/art-safe/preview");
  });

  it("fails closed on 403 and removes all stale detail and actions", async () => {
    mockMatchMedia();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 403 }));
    renderPanel({
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_accessible",
      generic_type: "document",
      ordinal: 1,
      safe_label: "Secret board notes",
      safe_query: "secret query",
      safe_preview: "secret preview",
      safe_url: "/secret-download",
      safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
    });

    fireEvent.click(screen.getByRole("button", { name: "open" }));
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/暂无来源数据/)).toBeInTheDocument());
    expect(within(dialog).getByText(/来源详情和操作已隐藏/)).toBeInTheDocument();
    expect(dialog.textContent).not.toContain("可能原因");
    expect(dialog.textContent).not.toContain("Secret board notes");
    expect(dialog.textContent).not.toContain("secret query");
    expect(dialog.textContent).not.toContain("secret preview");
    expect(dialog.querySelector("a")).toBeNull();
  });

  it("does not accept a legacy raw identity or fetch it", async () => {
    mockMatchMedia();
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    renderPanel({
      id: "art_secret",
      tool: "bi_query",
      query: "sensitive query",
      artifact_id: "art_secret",
    });

    fireEvent.click(screen.getByRole("button", { name: "open" }));
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/暂无来源数据/)).toBeInTheDocument());
    expect(dialog.textContent).not.toContain("art_secret");
    expect(dialog.textContent).not.toContain("sensitive query");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("returns focus to the citation trigger after close", async () => {
    mockMatchMedia();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    renderPanel({
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_accessible",
      generic_type: "other_source",
      ordinal: 1,
      safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
    });
    const trigger = screen.getByRole("button", { name: "open" });
    trigger.focus();
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });
});

describe("SourcePanel renders document excerpts as markdown", () => {
  const MD = [
    "## 华东区经营月报",
    "",
    "- 门店 **42** 家",
    "- 营业额 `3.86` 亿元",
    "",
    "| 区域 | 营业额 |",
    "| --- | --- |",
    "| 上海 | 1.2 |",
    "",
    "```sql",
    "select 1",
    "```",
    "",
    "[原文](https://example.com/report)",
    "",
    "![像素](https://attacker.example/pixel.png)",
    "",
    "<script>window.__pwned = 1</script>",
    "<img src=x onerror=\"window.__pwned = 2\">",
  ].join("\n");
  const ref = (extra: Partial<SourceRef> = {}): SourceRef => ({
    id: ALIAS,
    source_alias: ALIAS,
    access_state: "linked_accessible",
    generic_type: "document",
    ordinal: 1,
    safe_label: "月报.md",
    safe_preview: MD,
    safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
    ...extra,
  });

  it("renders lists, bold, tables, code and links; script and onerror never reach the page or run", async () => {
    mockMatchMedia(false);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ family: "document", mode: "text", text: null }) }));
    (window as unknown as { __pwned?: number }).__pwned = undefined;
    renderPanel(ref());
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    const md = await screen.findByTestId("source-panel-markdown");
    expect(within(md).getByRole("heading", { name: "华东区经营月报" })).toBeInTheDocument();
    expect(md.querySelectorAll("li")).toHaveLength(2);
    expect(within(md).getByText("42").tagName).toBe("STRONG");
    expect(within(md).getByText("3.86").tagName).toBe("CODE");
    expect(md.querySelector("table td")).toHaveTextContent("上海");
    expect(md.querySelector("pre code")).toHaveTextContent("select 1");
    const link = within(md).getByRole("link", { name: "原文" });
    expect(link).toHaveAttribute("href", "https://example.com/report");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(md.querySelector("script, img, [onerror]")).toBeNull();
    // Images do not auto-load: render links with alt text
    const image = md.querySelector("[data-md-image]");
    expect(image).toHaveTextContent("像素");
    expect(image).toHaveAttribute("href", "https://attacker.example/pixel.png");
    expect(image).toHaveAttribute("rel", "noopener noreferrer");
    expect(md.textContent).not.toContain("##");
    expect(md.textContent).not.toContain("**");
    await new Promise((r) => setTimeout(r, 0));
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(document.querySelector(".legacy-source-panel-preview")).toBeNull();
  });

  it("javascript: 链接被去掉（不可点）", async () => {
    mockMatchMedia(false);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ family: "document", mode: "text", text: null }) }));
    renderPanel(ref({ safe_preview: "[点我](javascript:alert(1))" }));
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    const md = await screen.findByTestId("source-panel-markdown");
    const a = md.querySelector("a");
    expect(a?.getAttribute("href") ?? "").not.toMatch(/javascript:/i);
  });

  it("整段 JSON 对象原样显示；行数据渲染成表格", async () => {
    mockMatchMedia(false);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ family: "document", mode: "text", text: null }) }));
    renderPanel(ref({ safe_preview: '{"region": "East", "revenue": 42}' }));
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    await waitFor(() => expect(document.querySelector(".legacy-source-panel-preview")).toHaveTextContent('"region"'));
    expect(screen.queryByTestId("source-panel-markdown")).toBeNull();
    cleanup();
    renderPanel(ref({ safe_preview: undefined, safe_preview_columns: ["region"], safe_preview_rows: [{ region: "West" }], safe_total_rows: 1 }));
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    const table = await screen.findByTestId("source-panel-table");
    expect(table.querySelector("td")).toHaveTextContent("West");
    expect(document.querySelector(".legacy-source-panel-preview")).toBeNull();
    expect(screen.queryByTestId("source-panel-markdown")).toBeNull();
  });

  it("超过 2000 字截断并写明（与原来一致）", async () => {
    mockMatchMedia(false);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ family: "document", mode: "text", text: null }) }));
    renderPanel(ref({ safe_preview: `**开头**\n\n${"长".repeat(2100)}` }));
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    const md = await screen.findByTestId("source-panel-markdown");
    expect(screen.getByRole("dialog").querySelector(".source-panel-md-truncated")).not.toBeNull();
    expect((md.querySelector(".v2-md")!.textContent ?? "").length).toBeLessThanOrEqual(2000);
  });
});

describe("SourcePanel uses the shared Dialog drawer", () => {
  const REF: SourceRef = {
    id: ALIAS,
    source_alias: ALIAS,
    access_state: "linked_accessible",
    generic_type: "query_result",
    ordinal: 1,
    safe_label: "Revenue source",
    safe_query: "Revenue by region",
    safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
    safe_preview_columns: ["region", "revenue"],
    safe_preview_rows: [{ region: "West", revenue: "41" }],
    safe_total_rows: 1,
  };

  it("is a modal drawer on body named after the source; Escape closes it and returns focus to the marker", async () => {
    mockMatchMedia(false);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ family: "table", mode: "rows", columns: [], rows: [] }) }));
    const { container } = renderPanel(REF);
    const trigger = screen.getByRole("button", { name: "open" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveClass("settings-dialog", "settings-dialog--drawer");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(container.contains(dialog)).toBe(false);
    expect(document.querySelector("aside.legacy-source-panel")).toBeNull();
    expect(within(dialog).getByTestId("source-panel-drawer")).toHaveTextContent("来源 · [1]");
    await waitFor(() => expect(screen.getByRole("dialog", { name: "Revenue source" })).toBe(dialog));
    expect(within(dialog).getByText(/West/)).toBeInTheDocument();
    fireEvent.keyDown(document.activeElement ?? dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("reveals no details when access is denied (403 fails closed)", async () => {
    mockMatchMedia(false);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 403 }));
    renderPanel({ ...REF, safe_preview: "secret preview", safe_url: "/secret-download" });
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/暂无来源数据/)).toBeInTheDocument());
    expect(dialog.textContent).not.toContain("Revenue source");
    expect(dialog.textContent).not.toContain("secret preview");
    expect(dialog.querySelector("a")).toBeNull();
  });

  it("is a bottom sheet on narrow screens", async () => {
    mockMatchMedia(true);
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));
    renderPanel(REF);
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveAttribute("data-dialog-presentation", "sheet"));
    expect(document.querySelector(".legacy-source-panel-scrim")).toBeNull();
  });
});

describe("SourcePanel「在数据中打开」走 i18n（原先写死英文 Open in Data）", () => {
  it.each([
    ["zh-CN", "在数据中打开"],
    ["en-US", "Open in Data"],
  ] as const)("%s：未开成果工作区时按当前语言显示", async (locale, label) => {
    mockMatchMedia();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ family: "text", mode: "text", text: "preview" }) }));
    function Opener() {
      const { open } = useSourcePanel();
      return (
        <button
          type="button"
          onClick={(event) => open({
            sourceRef: {
              id: ALIAS,
              source_alias: ALIAS,
              access_state: "linked_accessible",
              generic_type: "query_result",
              ordinal: 1,
              safe_label: "Revenue source",
              safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
              artifact_id: "art_safe",
            } as SourceRef,
            sourceQuality: "linked_accessible",
            sourceIndex: 0,
            display: "[1]",
            value: "",
            unit: "",
            highlight: "",
          }, event.currentTarget)}
        >
          open
        </button>
      );
    }
    renderIntl(
      <SourceAccessRegistryProvider>
        <CanvasContextProvider sections={[]} sourceRefs={[]}>
          <SourcePanelProvider>
            <Opener />
            <SourcePanel />
          </SourcePanelProvider>
        </CanvasContextProvider>
      </SourceAccessRegistryProvider>,
      { locale },
    );
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    const action = await within(screen.getByRole("dialog")).findByRole("button", { name: label });
    expect(action).toHaveClass("legacy-source-panel-action");
    expect(within(screen.getByRole("dialog")).queryByText("Open in Data")).toBe(locale === "en-US" ? action : null);
  });
});

describe("SourcePanel「在文件中打开」与预览截断后缀走 i18n（原先写死英文）", () => {
  it.each([
    ["zh-CN", "在文件中打开", "…（已截断）"],
    ["en-US", "Open in Files", "…(truncated)"],
  ] as const)("%s：按当前语言显示", async (locale, filesLabel, truncated) => {
    mockMatchMedia();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "x".repeat(2100) }),
    }));
    function Opener() {
      const { open } = useSourcePanel();
      return (
        <button
          type="button"
          onClick={(event) => open({
            sourceRef: {
              id: ALIAS,
              source_alias: ALIAS,
              access_state: "linked_accessible",
              generic_type: "query_result",
              ordinal: 1,
              safe_label: "Revenue source",
              safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
              kb_file_id: "kf_safe",
            } as SourceRef,
            sourceQuality: "linked_accessible",
            sourceIndex: 0,
            display: "[1]",
            value: "",
            unit: "",
            highlight: "",
          }, event.currentTarget)}
        >
          open
        </button>
      );
    }
    renderIntl(
      <SourceAccessRegistryProvider>
        <CanvasContextProvider sections={[]} sourceRefs={[]}>
          <SourcePanelProvider>
            <Opener />
            <SourcePanel />
          </SourcePanelProvider>
        </CanvasContextProvider>
      </SourceAccessRegistryProvider>,
      { locale },
    );
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    const dialog = screen.getByRole("dialog");
    const link = await within(dialog).findByRole("link", { name: filesLabel });
    expect(link).toHaveClass("legacy-source-panel-action");
    // Render file bodies as markdown; the truncation notice is a separate line below the excerpt
    const note = dialog.querySelector(".source-panel-md-truncated") as HTMLElement;
    expect(note.textContent).toBe(truncated);
    if (locale === "zh-CN") {
      expect(within(dialog).queryByText("Open in Files")).toBeNull();
      expect(dialog.textContent).not.toContain("(truncated)");
    }
  });
});

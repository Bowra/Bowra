import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import "@testing-library/jest-dom/vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { HttpStatusError } from "@/lib/api-client";
import { AIRewriteDiffBar } from "./ai-rewrite-diff-bar";
import type { useAIRewrite } from "@/lib/use-ai-rewrite";

type RewriteHandle = ReturnType<typeof useAIRewrite>;

afterEach(() => cleanup());

describe("AIRewriteDiffBar", () => {
  it("describes detached source links as numbers to check", () => {
    const rewrite = {
      state: {
        active: false,
        preset: "concise",
        reviewing: true,
        newText: "changed",
        unverified: true,
        error: null,
      },
      start: vi.fn(), cancel: vi.fn(), accept: vi.fn(), reject: vi.fn(), clearError: vi.fn(),
    } as unknown as RewriteHandle;

    renderIntl(<AIRewriteDiffBar rewrite={rewrite} editor={null} />, { locale: "en-US" });
    const bar = screen.getByRole("group", { name: /numbers, dates, or source references/ });
    expect(bar.textContent).toContain("Numbers to check");
    expect(bar.textContent).toContain("chart data has not been recalculated");
    expect(bar.textContent).not.toMatch(/verified figure|marked unverified/i);
  });

  it("shows rewrite errors and lets the user close them", () => {
    const clearError = vi.fn();
    const rewrite = {
      state: {
        active: false,
        preset: "concise",
        reviewing: false,
        newText: "",
        unverified: false,
        error: "unavailable",
      },
      start: vi.fn(),
      cancel: vi.fn(),
      accept: vi.fn(),
      reject: vi.fn(),
      clearError,
    } as unknown as RewriteHandle;

    renderIntl(<AIRewriteDiffBar rewrite={rewrite} editor={null} />);

    expect(screen.getByRole("alert").textContent).toContain("改写失败");
    expect(screen.getByRole("alert").textContent).toContain("改写服务暂时不可用");
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(clearError).toHaveBeenCalledTimes(1);
  });

  describe("retry on error (batch 4a)", () => {
    const errored = (retry = vi.fn()) => ({
      state: {
        active: false,
        preset: "concise",
        reviewing: false,
        newText: "",
        unverified: false,
        error: "unavailable",
      },
      start: vi.fn(), cancel: vi.fn(), accept: vi.fn(), reject: vi.fn(), clearError: vi.fn(), retry,
    } as unknown as RewriteHandle);

    it("the error bar offers retry, which resends the same request", () => {
      const retry = vi.fn();
      renderIntl(<AIRewriteDiffBar rewrite={errored(retry)} editor={null} />);
      fireEvent.click(screen.getByRole("button", { name: "重试" }));
      expect(retry).toHaveBeenCalledTimes(1);
    });
  });
});

describe("AIRewriteDiffBar：失败说明不带状态码与原始响应", () => {
  const REQUEST_ID = "0123456789abcdef0123456789abcdef";
  const cause = () =>
    Object.assign(new HttpStatusError("rewrite", new Response(null, { status: 502, headers: { "x-request-id": REQUEST_ID } })), {
      body: "<html>502 Bad Gateway</html>",
    });

  it.each([
    ["zh-CN", "改写服务暂时不可用", "复制诊断"],
    ["en-US", "temporarily unavailable", "Copy diagnostics"],
    ["de-DE", "vorübergehend nicht verfügbar", "Diagnoseinformationen kopieren"],
    ["fr-FR", "temporairement indisponible", "Copier le diagnostic"],
  ] as const)("%s: localized reason, Copy diagnostics outside the alert", (locale, text, copy) => {
    const rewrite = {
      state: { active: false, preset: "concise", reviewing: false, newText: "", unverified: false, error: "unavailable", errorCause: cause() },
      start: vi.fn(), cancel: vi.fn(), accept: vi.fn(), reject: vi.fn(), clearError: vi.fn(), retry: vi.fn(),
    } as unknown as RewriteHandle;

    renderIntl(<AIRewriteDiffBar rewrite={rewrite} editor={null} />, { locale });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(text);
    expect(alert.textContent).not.toMatch(/502|rewrite:|Bad Gateway|html/);
    const button = screen.getByRole("button", { name: copy });
    expect(alert.contains(button)).toBe(false);
  });
});

describe("AIRewriteDiffBar 图标颜色（原内联 color: var(--c-err) / var(--c-accent) 改修饰类）", () => {
  function handle(error: string | null) {
    return {
      state: {
        active: false,
        preset: "concise",
        reviewing: !error,
        newText: error ? "" : "changed",
        unverified: false,
        error,
      },
      start: vi.fn(), cancel: vi.fn(), accept: vi.fn(), reject: vi.fn(), clearError: vi.fn(),
    } as unknown as RewriteHandle;
  }

  it.each([
    ["改写失败", "unavailable", ["legacy-rc-result-spark", "legacy-rc-result-spark--error"]],
    ["改写完成", null, ["legacy-rc-result-spark"]],
  ])("%s", (_label, error, classNames) => {
    renderIntl(<AIRewriteDiffBar rewrite={handle(error)} editor={null} />);
    const bar = document.querySelector(".rc-result-bar") as HTMLElement;
    expect(bar.classList.contains("legacy-rc-result-bar")).toBe(true);
    expect(bar.getAttribute("style")).toBeNull();
    const spark = bar.querySelector("svg") as SVGElement;
    for (const name of classNames) expect(spark.classList.contains(name)).toBe(true);
    expect(spark.classList.contains("legacy-rc-result-spark--error")).toBe(error !== null);
    // Colour comes from the modifier class, never from an inline style.
    expect(spark.style.color).toBe("");
  });
});

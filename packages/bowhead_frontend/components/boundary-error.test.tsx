import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import enErrorCatalog from "../../../config/i18n/errors/en-US.json";
import zhErrorCatalog from "../../../config/i18n/errors/zh-CN.json";
import enMessages from "@/messages/en-US.json";
import zhMessages from "@/messages/zh-CN.json";

import { boundaryErrorCopy } from "@/lib/boundary-error-copy";

import { BoundaryErrorPage } from "./boundary-error";

const { capability } = vi.hoisted(() => ({
  capability: { enabled: true },
}));

vi.mock("@/lib/use-error-diagnostics-capability", () => ({
  useErrorDiagnosticsCapability: () => capability.enabled,
}));

describe("BoundaryErrorPage", () => {
  beforeEach(() => {
    capability.enabled = true;
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_VERSION", "1.2.3");
    vi.spyOn(console, "error").mockImplementation(() => undefined);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("renders a localized route recovery page without raw error text", async () => {
    const reset = vi.fn();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const error = Object.assign(
      new Error(
        "password=secret at https://internal.example/private.js",
      ),
      { digest: "next-digest-123" },
    );

    render(
      <BoundaryErrorPage
        error={error}
        reset={reset}
        surface="route"
        locale="zh-CN"
      />,
    );

    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByText("这个页面暂时无法显示")).toBeTruthy();
    expect(document.body.textContent).not.toContain("password=secret");
    expect(document.body.textContent).not.toContain("internal.example");
    expect(
      screen.getByTestId("error-diagnostics").hasAttribute("open"),
    ).toBe(false);

    fireEvent.click(screen.getByText("查看技术详情"));
    expect(screen.getByText("route_render_failed")).toBeTruthy();
    expect(screen.getByText("route_error_boundary_caught")).toBeTruthy();
    expect(screen.getByText("next-digest-123")).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "复制诊断信息" }),
    );
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toContain("surface: route");
    expect(copied).toContain("error_code: route_render_failed");
    expect(copied).toContain("digest: next-digest-123");
    expect(copied).not.toContain("password=secret");
    expect(copied).not.toContain("internal.example");

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(reset).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain(
      "password=secret",
    );
  });

  it("uses the finite chunk projection without inspecting its message", () => {
    render(
      <BoundaryErrorPage
        error={Object.assign(
          new Error("Loading chunk from https://secret.example"),
          { name: "ChunkLoadError" },
        )}
        reset={vi.fn()}
        surface="route"
        locale="en-US"
      />,
    );

    expect(screen.getByText("Some page resources could not be loaded")).toBeTruthy();
    expect(screen.getAllByText("client_chunk_load_failed")).toHaveLength(2);
    expect(
      screen.getByRole("button", { name: "Reload page" }),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Try again" }),
    ).toBeNull();
    expect(document.body.textContent).not.toContain("secret.example");
  });

  it("keeps the root boundary public-neutral and independent of providers", () => {
    render(
      <BoundaryErrorPage
        error={new Error("raw stack and private path")}
        reset={vi.fn()}
        surface="global"
        locale="en-US"
      />,
    );

    expect(screen.getByText("Bowhead hit an unexpected error")).toBeTruthy();
    expect(screen.getByText("global_render_failed")).toBeTruthy();
    expect(screen.getByText("global_error_boundary_caught")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Go home" })).toBeNull();
    expect(document.body.textContent).not.toContain("raw stack");
  });

  it("keeps diagnostics hidden when the runtime capability is off", () => {
    capability.enabled = false;
    render(
      <BoundaryErrorPage
        error={new Error("raw detail")}
        reset={vi.fn()}
        surface="route"
        locale="en-US"
      />,
    );

    expect(screen.getByText("This page could not be displayed")).toBeTruthy();
    expect(screen.queryByTestId("error-diagnostics")).toBeNull();
    expect(document.body.textContent).not.toContain("raw detail");
  });

  it("keeps the provider-free dictionary aligned with both i18n sources", () => {
    const en = boundaryErrorCopy("en-US");
    const zh = boundaryErrorCopy("zh-CN");

    // Since 7162e70ee the boundary copy also carries the localized surface
    // names, which the provider catalogs keep under errors.surfaces.
    const { surfaceNames: enSurfaceNames, ...enDiagnostics } = en.diagnostics;
    const { surfaceNames: zhSurfaceNames, ...zhDiagnostics } = zh.diagnostics;
    expect(enDiagnostics).toEqual(enMessages.errors.diagnostics);
    expect(zhDiagnostics).toEqual(zhMessages.errors.diagnostics);
    expect(enSurfaceNames).toEqual({
      global: enMessages.errors.surfaces.global,
      route: enMessages.errors.surfaces.route,
    });
    expect(zhSurfaceNames).toEqual({
      global: zhMessages.errors.surfaces.global,
      route: zhMessages.errors.surfaces.route,
    });
    expect(en.failureMessages.routeRenderFailed).toBe(
      enErrorCatalog.route_render_failed,
    );
    expect(en.failureMessages.globalRenderFailed).toBe(
      enErrorCatalog.global_render_failed,
    );
    expect(en.failureMessages.chunkLoadFailed).toBe(
      enErrorCatalog.client_chunk_load_failed,
    );
    expect(zh.failureMessages.routeRenderFailed).toBe(
      zhErrorCatalog.route_render_failed,
    );
    expect(zh.failureMessages.globalRenderFailed).toBe(
      zhErrorCatalog.global_render_failed,
    );
    expect(zh.failureMessages.chunkLoadFailed).toBe(
      zhErrorCatalog.client_chunk_load_failed,
    );
  });
});

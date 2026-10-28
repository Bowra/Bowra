// credential-readiness R9: visual-parsing fallback notice at upload entry (PRD §6.4, highest-priority contextual notice).
import "@testing-library/jest-dom/vitest";

import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { KbCapabilities } from "@/lib/kb-capabilities-api";

const mocks = vi.hoisted(() => ({
  fetchKbCapabilities: vi.fn(),
}));

vi.mock("@/lib/kb-capabilities-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-capabilities-api")>();
  return {
    ...actual,
    fetchKbCapabilities: (...args: unknown[]) => mocks.fetchKbCapabilities(...args),
  };
});

// me fetch uses appFetch; interrupt the network, as the me=null branch does not affect this test's focus
vi.mock("@/lib/base-path", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/base-path")>();
  return {
    ...actual,
    appFetch: vi.fn().mockRejectedValue(new Error("offline")),
  };
});

import { KbUploadModal } from "./kb-upload-modal";

function caps(vision: KbCapabilities["kb_vision"]): KbCapabilities {
  return { kb_rerank: "enabled", kb_vision: vision, kb_row_extractor: "enabled" };
}

describe("KbUploadModal · kb_vision 现场提示", () => {
  beforeEach(() => {
    mocks.fetchKbCapabilities.mockReset();
  });
  afterEach(cleanup);

  it("kb_vision=disabled 时在上传入口显示「未开启」警示", async () => {
    mocks.fetchKbCapabilities.mockResolvedValue(caps("disabled"));
    renderIntl(<KbUploadModal defaultScope="personal" onClose={() => {}} onUploaded={() => {}} />);
    await waitFor(() => {
      expect(screen.getByTestId("kb-vision-degraded-hint")).toHaveTextContent("视觉解析未开启");
    });
    expect(screen.getByTestId("kb-vision-degraded-hint")).toHaveTextContent("扫描件");
  });

  it("kb_vision=invalid 时显示「配置无效」变体", async () => {
    mocks.fetchKbCapabilities.mockResolvedValue(caps("invalid"));
    renderIntl(<KbUploadModal defaultScope="personal" onClose={() => {}} onUploaded={() => {}} />);
    await waitFor(() => {
      expect(screen.getByTestId("kb-vision-degraded-hint")).toHaveTextContent("视觉解析配置无效");
    });
  });

  it("kb_vision=enabled 时不出现提示", async () => {
    mocks.fetchKbCapabilities.mockResolvedValue(caps("enabled"));
    renderIntl(<KbUploadModal defaultScope="personal" onClose={() => {}} onUploaded={() => {}} />);
    await waitFor(() => {
      expect(mocks.fetchKbCapabilities).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("kb-vision-degraded-hint")).not.toBeInTheDocument();
  });

  it("能力状态取不到（null）时静默无提示", async () => {
    mocks.fetchKbCapabilities.mockResolvedValue(null);
    renderIntl(<KbUploadModal defaultScope="personal" onClose={() => {}} onUploaded={() => {}} />);
    await waitFor(() => {
      expect(mocks.fetchKbCapabilities).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("kb-vision-degraded-hint")).not.toBeInTheDocument();
  });
});

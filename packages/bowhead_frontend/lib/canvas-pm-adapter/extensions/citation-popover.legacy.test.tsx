/**
 * Chat workbench and report canvas · batch 1: notice spacing in the citation-source popover's no-source card.
 * Original inline style: marginTop: valueDisplay ? 10 : 0. Compare each branch after moving to modifier classes (styles/workbench-legacy.test.ts locks class values).
 */
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { SourcePanelProvider } from "@/components/canvas/source-panel";
import { SourceAccessRegistryProvider } from "@/lib/source-access-registry";
import { CanvasContextProvider } from "../canvas-context";
import { CitationHoverManager, POPOVER_OPEN_DELAY_MS } from "./citation-popover";
/** D13: hover previews open after a short dwell (WCAG 1.4.13). */
async function dwell() {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, POPOVER_OPEN_DELAY_MS + 20)); });
}


vi.stubGlobal("matchMedia", (query: string) => ({
  matches: false,
  media: query,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
}));

afterEach(cleanup);

function Harness({ withValue }: { withValue: boolean }) {
  const root = useRef<HTMLDivElement | null>(null);
  return (
    <SourceAccessRegistryProvider>
      <SourcePanelProvider>
        <CanvasContextProvider sections={[]}>
          <div ref={root}>
            <p>
              营业额
              <sup
                data-citation-kind="num"
                data-source-id="s_missing"
                {...(withValue ? { "data-value": "962.7", "data-unit": "亿元" } : {})}
                tabIndex={0}
              >
                [1]
              </sup>
            </p>
          </div>
          <CitationHoverManager rootRef={root} />
        </CanvasContextProvider>
      </SourcePanelProvider>
    </SourceAccessRegistryProvider>
  );
}

describe("无来源卡的提示条（原 marginTop: valueDisplay ? 10 : 0）", () => {
  it.each([
    [true, "rc-prov-affirm warn legacy-rc-prov-affirm--gap"],
    [false, "rc-prov-affirm warn legacy-rc-prov-affirm--flush"],
  ])("带数值=%s → %s", async (withValue, className) => {
    renderIntl(<Harness withValue={withValue} />);
    fireEvent.mouseOver(screen.getByText("[1]"));
    await dwell();
    const affirm = document.querySelector(".citation-popover .rc-prov-affirm") as HTMLElement;
    expect(affirm).not.toBeNull();
    expect(affirm.className).toBe(className);
    expect(affirm.getAttribute("style")).toBeNull();
    expect(Boolean(document.querySelector(".citation-popover .rc-prov-value"))).toBe(withValue);
  });
});

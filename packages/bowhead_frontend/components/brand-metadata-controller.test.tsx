import { StrictMode } from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BRAND_METADATA_FAVICON_ID,
  BRAND_METADATA_TITLE_ID,
} from "@/lib/brand-metadata";
import { registerPageTitle } from "@/lib/page-title-store";
import { canonicalEffectiveThemeFallback } from "@/lib/theme-config";

const state = vi.hoisted(() => ({
  bundle: null as ReturnType<
    typeof canonicalEffectiveThemeFallback
  > | null,
}));

vi.mock("@/components/brand-provider", () => ({
  useBrand: () => ({
    bundle: state.bundle,
    brand: state.bundle?.brand,
  }),
}));

import { BrandMetadataController } from "./brand-metadata-controller";

function customBundle() {
  const bundle = canonicalEffectiveThemeFallback();
  bundle.team = {
    id: "team_acme",
    themeRevision: 3,
    revisionId: `tbr_${"a".repeat(24)}`,
    configurationType: "custom",
  };
  bundle.brand = {
    ...bundle.brand!,
    applicationName: "Acme Intelligence",
    logoAlt: "Acme Intelligence",
    favicon: { kind: "generatedInitial", text: "A" },
  };
  return bundle;
}

afterEach(() => {
  cleanup();
  state.bundle = null;
  document.getElementById(BRAND_METADATA_TITLE_ID)?.remove();
  document.getElementById(BRAND_METADATA_FAVICON_ID)?.remove();
});

describe("BrandMetadataController", () => {
  it("keeps one pair of owned nodes through the Strict Mode lifecycle", () => {
    state.bundle = canonicalEffectiveThemeFallback();

    const view = render(
      <StrictMode>
        <BrandMetadataController />
      </StrictMode>,
    );

    expect(document.querySelectorAll(`#${BRAND_METADATA_TITLE_ID}`)).toHaveLength(
      1,
    );
    expect(
      document.querySelectorAll(`#${BRAND_METADATA_FAVICON_ID}`),
    ).toHaveLength(1);
    view.unmount();
    expect(document.getElementById(BRAND_METADATA_TITLE_ID)).not.toBeNull();
    expect(document.getElementById(BRAND_METADATA_FAVICON_ID)).not.toBeNull();
  });

  it("composes registered page titles with the active application name", () => {
    state.bundle = canonicalEffectiveThemeFallback();
    render(<BrandMetadataController />);

    let unregister: () => void = () => undefined;
    act(() => {
      unregister = registerPageTitle("canvas", "Q3 营收").unregister;
    });
    expect(document.getElementById(BRAND_METADATA_TITLE_ID)?.textContent).toBe(
      "Q3 营收 · Bowhead",
    );

    act(() => unregister());
    expect(document.getElementById(BRAND_METADATA_TITLE_ID)?.textContent).toBe(
      "Bowhead — Verified Intelligence",
    );
  });

  it("reuses the owned nodes across custom, safe-mode and restore changes", () => {
    state.bundle = customBundle();
    const view = render(<BrandMetadataController />);
    const title = document.getElementById(BRAND_METADATA_TITLE_ID);
    const favicon = document.getElementById(BRAND_METADATA_FAVICON_ID);

    expect(title?.textContent).toBe(
      "Acme Intelligence — Verified Intelligence",
    );
    expect(favicon?.getAttribute("href")).toMatch(/^data:image\/svg\+xml/);

    act(() => {
      state.bundle = canonicalEffectiveThemeFallback();
      view.rerender(<BrandMetadataController />);
    });
    expect(document.getElementById(BRAND_METADATA_TITLE_ID)).toBe(title);
    expect(document.getElementById(BRAND_METADATA_FAVICON_ID)).toBe(favicon);
    expect(title?.textContent).toBe("Bowhead — Verified Intelligence");
    expect(favicon?.getAttribute("href")).toMatch(/\/bowhead-icon\.svg$/);

    act(() => {
      state.bundle = customBundle();
      view.rerender(<BrandMetadataController />);
    });
    expect(document.getElementById(BRAND_METADATA_TITLE_ID)).toBe(title);
    expect(document.getElementById(BRAND_METADATA_FAVICON_ID)).toBe(favicon);
    expect(title?.textContent).toBe(
      "Acme Intelligence — Verified Intelligence",
    );
  });
});

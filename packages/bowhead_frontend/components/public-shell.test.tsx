import { PRODUCT_NAME } from "@/lib/product-identity";
import "@testing-library/jest-dom/vitest";
import { readFileSync } from "node:fs";
import path from "node:path";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BrandedText } from "./brand-identity";
import { PublicBrandProvider } from "./brand-provider";
import { PublicShell, useBrandedTranslations } from "./public-shell";

const FIXTURE = JSON.parse(
  readFileSync(path.resolve(__dirname, "../e2e/fixtures/public-brand-custom.json"), "utf-8"),
) as { brand: Record<string, unknown> };

const MESSAGES = {
  probe: {
    enter: "进入 {applicationName}",
    joined: "{inviter} 邀请你加入「{team}」· {applicationName}",
  },
  topbar: { switch_language_current: "切换语言（当前 {locale}）" },
  invite: { progress: { position: "第 {current} 步，共 {total} 步" } },
};

type Brand = Record<string, unknown>;

function brandNamed(name: string, overrides: Brand = {}) {
  const value = JSON.parse(JSON.stringify(FIXTURE)) as { brand: Brand };
  value.brand = { ...value.brand, applicationName: name, logoAlt: name, ...overrides };
  return value;
}

function withBrand(brand: unknown, ui: React.ReactNode) {
  (window as Window & { __BOWHEAD_PUBLIC_BRAND__?: unknown }).__BOWHEAD_PUBLIC_BRAND__ = brand;
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={MESSAGES}>
      <PublicBrandProvider>{ui}</PublicBrandProvider>
    </NextIntlClientProvider>,
  );
}

function Probe({ inviter = "李华", team = "北辰零售" }: { inviter?: string; team?: string }) {
  const t = useBrandedTranslations("probe");
  return (
    <>
      <p data-testid="enter">{t("enter")}</p>
      <p data-testid="joined">{t("joined", { inviter, team })}</p>
    </>
  );
}

afterEach(() => {
  cleanup();
  (window as Window & { __BOWHEAD_PUBLIC_BRAND__?: unknown }).__BOWHEAD_PUBLIC_BRAND__ = undefined;
});

describe("useBrandedTranslations", () => {
  it("puts an application name that itself contains Bowhead in once", () => {
    withBrand(brandNamed("Bowhead China"), <Probe />);
    expect(screen.getByTestId("enter")).toHaveTextContent(/^进入 Bowhead China$/);
  });

  it("keeps replacement-pattern characters in the application name literal", () => {
    withBrand(brandNamed("Acme $& $1 $$"), <Probe />);
    expect(screen.getByTestId("enter")).toHaveTextContent(/^进入 Acme \$& \$1 \$\$$/);
  });

  it("leaves Bowhead inside interpolated team and inviter names untouched", () => {
    withBrand(brandNamed("Northstar Data"), <Probe inviter="Bowhead 管理员" team="Bowhead 华东" />);
    expect(screen.getByTestId("joined")).toHaveTextContent(
      "Bowhead 管理员 邀请你加入「Bowhead 华东」· Northstar Data",
    );
  });

  it("uses the product name when nothing is injected", () => {
    withBrand(undefined, <Probe />);
    expect(screen.getByTestId("enter")).toHaveTextContent(new RegExp(`^进入 ${PRODUCT_NAME}$`));
  });
});

describe("BrandedText", () => {
  it("does not treat $ sequences in the application name as replacement patterns", () => {
    withBrand(brandNamed("Acme $& Co"), <p data-testid="t"><BrandedText>{"打开 Bowhead"}</BrandedText></p>);
    expect(screen.getByTestId("t")).toHaveTextContent(/^打开 Acme \$& Co$/);
  });
});

describe("PublicShell brand row", () => {
  it("shows only the name, not the Bowhead mark, for a custom brand without a logo", () => {
    const text = { kind: "text", text: "Northstar Data" };
    withBrand(
      brandNamed("Northstar Data", { logoOnLight: text, logoOnDark: text }),
      <PublicShell title="登录" />,
    );
    const row = screen.getByTestId("public-brand");
    expect(row).toHaveTextContent(/^Northstar Data$/);
    expect(row.querySelector("svg, img")).toBeNull();
  });

  it("shows the Bowhead mark for the product default", () => {
    withBrand(undefined, <PublicShell title="登录" />);
    expect(screen.getByTestId("public-brand").querySelector("svg")).not.toBeNull();
  });
});

// Header, Language & Paging Spec §1c (D1): public pages use the same native-name table as the in-app menu,
// without "Automatic", and only write the NEXT_LOCALE cookie before one reload — no authenticated API calls.
describe("PublicShell language menu", () => {
  it("lists the four languages in their own names, checks the current one, and switches by cookie only", () => {
    const reload = vi.fn();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    Object.defineProperty(window, "location", { value: { ...window.location, reload }, writable: true, configurable: true });
    document.cookie = "NEXT_LOCALE=; path=/; max-age=0";
    withBrand(undefined, <PublicShell title="登录" />);
    const trigger = screen.getByRole("button", { name: "切换语言（当前 中文）" });
    expect(trigger).toHaveTextContent("中文");
    fireEvent.click(trigger);
    const options = screen.getAllByRole("menuitemradio");
    expect(options.map((option) => [option.textContent, option.getAttribute("lang")])).toEqual([
      ["中文", "zh-CN"],
      ["English", "en-US"],
      ["Deutsch", "de-DE"],
      ["Français", "fr-FR"],
    ]);
    expect(options[0]).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByText(/自动|Automatic/)).toBeNull();
    fireEvent.click(options[1]);
    expect(document.cookie).toContain("NEXT_LOCALE=en-US");
    expect(reload).toHaveBeenCalledTimes(1);
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });
});

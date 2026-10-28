import { BUILD_EDITION, PRODUCT_NAME } from "@/lib/product-identity";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { resetServerVersionCacheForTests } from "@/lib/app-version";
import { fetchMe, type MeUser } from "@/lib/auth-api";
import { _clearAllToasts, useToasts } from "@/lib/use-toast";
import { UserMenu } from "./user-menu";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("@/lib/auth-api", () => ({
  fetchMe: vi.fn(),
  logoutUser: vi.fn(),
}));
vi.mock("@/lib/canvas-navigation-guard", () => ({
  hasCanvasUnsavedChanges: () => false,
  confirmCanvasNavigation: () => Promise.resolve(true),
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push, replace: vi.fn() }),
}));
const languagePrefs = vi.hoisted(() => ({
  current: { status: "ready", ui: "zh-CN", output: "auto", teamLock: null as string | null },
}));
vi.mock("@/lib/use-language-preferences", () => ({
  useLanguagePreferences: () => ({
    ...languagePrefs.current,
    reload: vi.fn(),
    saveAnswerLanguage: vi.fn(),
    rememberUiLanguage: vi.fn(),
  }),
  answerFollowsQuestion: (value: string) => !["zh-CN", "en-US", "de-DE", "fr-FR"].includes(value),
}));
const me: MeUser = {
  id: "u_allen",
  email: "exampleuser@example.com",
  display_name: "Allen",
  avatar_url: null,
  team_id: "team_1",
  tier: "ultra",
  role: "user",
  team_role: "owner",
  status: "active",
  email_verified: true,
  credit_balance: 30_000,
  credit_quota: 30_000,
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  is_team_admin: true,
  workspace_count: 1,
  needs_onboarding: false,
};

describe("UserMenu avatar fallback", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    delete window.__BOWHEAD_BASE_PATH__;
  });

  it("loads the current user before the menu opens", async () => {
    vi.mocked(fetchMe).mockResolvedValue(me);

    renderIntl(<UserMenu initials="N" />);

    const trigger = screen.getByRole("button");
    expect(trigger.textContent).toBe("N");
    await waitFor(() => expect(trigger.textContent).toBe("A"));
  });

  it("reuses a user loaded by the parent shell", () => {
    renderIntl(<UserMenu initials="N" user={me} />);

    expect(screen.getByRole("button", { name: "账户" }).textContent).toBe("A");
    expect(fetchMe).not.toHaveBeenCalled();
  });

  it("makes the full expanded identity row the account trigger", () => {
    renderIntl(<UserMenu initials="N" user={me} showIdentity />);

    const trigger = screen.getByRole("button", { name: "账户" });
    expect(trigger.textContent).toContain("Allen");
    expect(trigger.textContent).toContain("负责人");

    fireEvent.click(trigger);
    expect(
      screen.getByRole("menuitem", { name: "设置" }),
    ).toBeTruthy();
  });

  it("falls back to the email initial when the display name is blank", async () => {
    vi.mocked(fetchMe).mockResolvedValue({ ...me, display_name: "  " });

    renderIntl(<UserMenu initials="N" />);

    const trigger = screen.getByRole("button");
    await waitFor(() => expect(trigger.textContent).toBe("E"));
  });

  it("retries a failed identity load when opened, but not when closed", async () => {
    vi.mocked(fetchMe).mockRejectedValue(new Error("offline"));

    renderIntl(<UserMenu initials="N" />);

    const trigger = screen.getByRole("button");
    await waitFor(() => expect(fetchMe).toHaveBeenCalledTimes(1));

    fireEvent.click(trigger);
    await waitFor(() => expect(fetchMe).toHaveBeenCalledTimes(2));

    fireEvent.click(trigger);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetchMe).toHaveBeenCalledTimes(2);
  });

  it("opens settings and the authorized console under the public-entry prefix", () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    renderIntl(<UserMenu initials="N" user={me} />);
    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    const settings = screen.getByRole("menuitem", { name: "设置" });
    expect(settings.tagName).toBe("A");
    expect(settings.getAttribute("href")).toBe("/bowhead/settings/profile/");
    expect(screen.getByRole("menuitem", { name: "管理控制台" }).getAttribute("href")).toBe("/bowhead/admin/");
    expect(screen.getByRole("menuitem", { name: "帮助中心" }).getAttribute("href")).toBe("/bowhead/help/");
    expect(screen.getByRole("menuitem", { name: "更新说明" }).getAttribute("href")).toBe("/bowhead/release-notes/");
    expect(screen.getByRole("menuitem", { name: /^语言/ })).toBeTruthy();
    expect(screen.getByRole("menu").classList.contains("app-floating-menu")).toBe(true);
    expect(settings.classList.contains("app-floating-menu-item")).toBe(true);
  });

  it("uses Title Case for English account-menu labels", () => {
    renderIntl(<UserMenu initials="N" user={me} />, { locale: "en-US" });

    fireEvent.click(screen.getByRole("button", { name: "Account" }));

    for (const label of [
      "Settings",
      "Administration Console",
      "Help Center",
      "Release Notes",
      "Sign Out",
    ]) {
      expect(screen.getByRole("menuitem", { name: label })).toBeTruthy();
    }
  });

  it("uses Title Case for the version action and language submenu link", () => {
    renderIntl(<UserMenu initials="N" user={me} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Account" }));
    expect(screen.getByRole("menuitem", { name: /Copy Version Details/ }).getAttribute("title")).toBe("Copy Version Details");
    fireEvent.keyDown(screen.getByRole("menuitem", { name: /^Language/ }), { key: "ArrowRight" });
    expect(screen.getByRole("menuitem", { name: /^Answer Language Settings/ })).toBeTruthy();
  });

  it("routes settings destinations without reloading the document", async () => {
    renderIntl(<UserMenu initials="N" user={me} />);

    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "设置" }));

    await waitFor(() =>
      expect(push).toHaveBeenCalledWith(
        "/settings/profile/",
      ),
    );
  });

  it("opens release notes from the account menu", async () => {
    renderIntl(<UserMenu initials="A" user={me} />);
    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "更新说明" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/release-notes/"));
  });

  it("opens the Help Center inside the product shell", async () => {
    renderIntl(<UserMenu initials="N" user={me} />);

    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "帮助中心" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/help/"));
  });

  it("routes a team manager to the administration directory", async () => {
    renderIntl(<UserMenu initials="N" user={me} />);
    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "管理控制台" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/admin/"));
  });

  it("shows Team readiness for a team manager", async () => {
    renderIntl(
      <UserMenu
        initials="N"
        user={{
          ...me,
          capabilities: { team_onboarding: true },
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    const setup = screen.getByRole("menuitem", { name: "团队启用状态" });
    expect(setup.getAttribute("href")).toBe("/setup/");
    fireEvent.click(setup);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/setup/"));
  });

  it("hides Team readiness when the role capability is absent", () => {
    renderIntl(
      <UserMenu
        initials="N"
        user={{ ...me, auth_mode: "prod" }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    expect(
      screen.queryByRole("menuitem", { name: "团队启用状态" }),
    ).toBeNull();
  });

  it("closes its containing drawer before navigating to personal settings", async () => {
    const onBeforePersonalSettingsNavigate = vi.fn();
    renderIntl(
      <UserMenu
        initials="N"
        user={me}
        onBeforePersonalSettingsNavigate={onBeforePersonalSettingsNavigate}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "设置" }));

    expect(onBeforePersonalSettingsNavigate).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/settings/profile/"));
  });

  it("keeps Administration hidden from members", () => {
    renderIntl(
      <UserMenu
        initials="N"
        user={{ ...me, team_role: "member", is_team_admin: false }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "账户" }));

    expect(
      screen.getByRole("menuitem", { name: "设置" }),
    ).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "管理控制台" })).toBeNull();
  });

  it("offers the audit administrator console only when the Business adapter is installed", () => {
    renderIntl(<UserMenu initials="N" user={{ ...me, team_role: "audit_admin", is_team_admin: false }} />);
    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    if (BUILD_EDITION === "business") {
      expect(screen.getByRole("menuitem", { name: "管理控制台" }).getAttribute("href")).toBe("/admin/");
    } else {
      expect(screen.queryByRole("menuitem", { name: "管理控制台" })).toBeNull();
    }
  });

  it("labels the account trigger and focuses the first menu action", async () => {
    vi.mocked(fetchMe).mockResolvedValue(me);
    renderIntl(<UserMenu initials="N" />);

    const trigger = screen.getByRole("button", { name: "账户" });
    fireEvent.click(trigger);

    await waitFor(() =>
      expect(
        screen.getByRole("menuitem", { name: "设置" }),
      ).toBe(document.activeElement),
    );
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(screen.getByRole("menuitem", { name: "管理控制台" })).toBe(
      document.activeElement,
    );
  });

  it("focuses the menu surface after a pointer-open", async () => {
    renderIntl(<UserMenu initials="N" user={me} />);

    fireEvent.click(screen.getByRole("button", { name: "账户" }), {
      detail: 1,
    });

    const menu = screen.getByRole("menu");
    const firstItem = screen.getByRole("menuitem", { name: "设置" });
    await waitFor(() => expect(menu).toBe(document.activeElement));
    expect(firstItem).not.toBe(document.activeElement);

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(firstItem).toBe(document.activeElement);
  });
});

// Behavior batch A (technical design “行为批 A：菜单”, Menus Spec §§2, 3, 6): Language is a submenu—
// parent row ends with current language + ›; desktop → opens, ← returns; narrow (< 640) account menu is a bottom sheet with in-sheet language drill-down.
describe("UserMenu 语言子菜单", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  function stubViewport(narrow: boolean) {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: narrow && query === "(max-width: 639.98px)",
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
  }

  it("桌面：父项带当前值与 ›，→ 打开子菜单、当前语言是选中的单选项，← 回到父项", async () => {
    stubViewport(false);
    renderIntl(<UserMenu initials="N" user={me} />);
    const trigger = screen.getByRole("button", { name: "账户" });
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    fireEvent.click(trigger, { detail: 0 });
    const language = await screen.findByRole("menuitem", { name: /^语言/ });
    expect(screen.queryByRole("menuitem", { name: "切换语言（当前 中文）" })).toBeNull();
    expect(language.getAttribute("aria-haspopup")).toBe("menu");
    expect(language.querySelector(".ui-menu-item-tail")?.textContent).toBe("中文");
    language.focus();
    fireEvent.keyDown(language, { key: "ArrowRight" });
    const sub = await screen.findByRole("menu", { name: "语言" });
    const zh = screen.getByRole("menuitemradio", { name: "中文" });
    expect(zh.getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("menuitemradio", { name: "English" }).getAttribute("aria-checked")).toBe("false");
    await waitFor(() => expect(document.activeElement).toBe(zh));
    fireEvent.keyDown(zh, { key: "ArrowLeft" });
    await waitFor(() => expect(sub.isConnected).toBe(false));
    await waitFor(() => expect(document.activeElement).toBe(language));
    expect(document.querySelector('[data-user-menu="true"]')).not.toBeNull();
  });

  it("窄屏：账户菜单是底部面板，点「语言」在同一面板内下钻，「‹ 账户」返回", async () => {
    stubViewport(true);
    renderIntl(<UserMenu initials="N" user={me} />);
    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    const account = document.querySelector<HTMLElement>('[data-user-menu="true"]')!;
    await waitFor(() => expect(account.className).toContain("is-sheet"));
    fireEvent.click(screen.getByRole("menuitem", { name: /^语言/ }));
    const sub = await screen.findByRole("menu", { name: "语言" });
    const sheet = document.querySelector(".ui-menu-sheet")!;
    expect(sheet.contains(sub)).toBe(true);
    expect(sheet.getAttribute("data-drilled")).toBe("true");
    const back = sub.querySelector<HTMLButtonElement>(".ui-menu-drill-back")!;
    expect(back.textContent).toBe("账户");
    fireEvent.click(back);
    await waitFor(() => expect(screen.queryByRole("menu", { name: "语言" })).toBeNull());
    expect(sheet.hasAttribute("data-drilled")).toBe(false);
    expect(document.querySelector('[data-user-menu="true"]')).not.toBeNull();
  });
});

// Header, Language & Paging Spec §1a (D1, D2): Automatic + four native names, and a last link row to the
// answer-language setting that shows its current value.
describe("UserMenu language submenu options", () => {
  afterEach(() => {
    cleanup();
    push.mockReset();
    languagePrefs.current = { status: "ready", ui: "zh-CN", output: "auto", teamLock: null };
  });

  async function openLanguage() {
    renderIntl(<UserMenu initials="N" user={me} />);
    fireEvent.click(screen.getByRole("button", { name: "账户" }));
    const language = await screen.findByRole("menuitem", { name: /^语言/ });
    fireEvent.click(language);
    return screen.findByRole("menu", { name: "语言" });
  }

  it("offers Automatic first, the languages in their own names, and the answer-language link last", async () => {
    const sub = await openLanguage();
    const radios = Array.from(sub.querySelectorAll('[role="menuitemradio"]'));
    expect(radios.map((item) => item.getAttribute("data-language-option"))).toEqual(["auto", "zh-CN", "en-US", "de-DE", "fr-FR"]);
    expect(radios[1].getAttribute("aria-checked")).toBe("true");
    const link = screen.getByRole("menuitem", { name: /^回答语言设置/ });
    // A link, not a radio option: no check column, current answer language at the end.
    expect(link.tagName).toBe("A");
    expect(link.getAttribute("aria-checked")).toBeNull();
    expect(link.getAttribute("href")).toBe("/settings/profile/#answer-language");
    expect(link.querySelector(".ui-menu-item-tail")?.textContent).toBe("跟随提问");
    fireEvent.click(link);
    expect(push).toHaveBeenCalledWith("/settings/profile/#answer-language");
  });

  it("checks Automatic when the saved interface language is auto, and shows a team-locked answer language", async () => {
    languagePrefs.current = { status: "ready", ui: "auto", output: "zh-CN", teamLock: "en-US" };
    const sub = await openLanguage();
    const auto = sub.querySelector('[data-language-option="auto"]');
    expect(auto?.getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("menuitem", { name: /^回答语言设置/ }).querySelector(".ui-menu-item-tail")?.textContent).toBe("English");
  });
});

describe("UserMenu icons and identity row (P2 batch 3)", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows team · role instead of the email on the identity row, with up/down chevrons", () => {
    renderIntl(<UserMenu initials="N" user={{ ...me, team_name: "商超零售" }} showIdentity />);

    const trigger = screen.getByRole("button", { name: "账户" });
    expect(trigger.querySelector("small")?.textContent).toBe("商超零售 · 负责人");
    expect(trigger.textContent).not.toContain("exampleuser@example.com");
    const caret = trigger.querySelector("svg:last-child") as SVGSVGElement;
    expect(caret.classList.contains("lucide-chevrons-up-down")).toBe(true);
    expect(caret.getAttribute("width")).toBe("14");
    expect(caret.classList.contains("unified-sidebar-caret")).toBe(true);
  });
});

describe("UserMenu version line (P2 batch 6)", () => {
  const writeText = vi.fn();

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    resetServerVersionCacheForTests();
    _clearAllToasts();
    writeText.mockReset();
  });

  function ToastProbe() {
    const toasts = useToasts();
    return <output data-testid="toasts">{toasts.map((item) => String(item.text)).join("|")}</output>;
  }

  function open(locale: "zh-CN" | "en-US" = "en-US") {
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_VERSION", "1.4.8");
    vi.stubEnv("NEXT_PUBLIC_BUILD_LOCALE", "en-US");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ version: "1.4.8" }), { status: 200 })),
    );
    writeText.mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    renderIntl(
      <>
        <UserMenu initials="N" user={me} />
        <ToastProbe />
      </>,
      { locale },
    );
    fireEvent.click(screen.getByRole("button", { name: locale === "en-US" ? "Account" : "账户" }));
  }

  it("is the last menu item, named by its visible label followed by the copy action", async () => {
    open();

    const items = screen.getAllByRole("menuitem");
    const version = items.at(-1) as HTMLElement;
    expect(version.textContent).toBe(`${PRODUCT_NAME} 1.4.8, Copy Version Details`);
    expect(version.getAttribute("title")).toBe("Copy Version Details");
    expect(screen.getByRole("menuitem", { name: new RegExp(`^${PRODUCT_NAME} 1\\.4\\.8`) })).toBe(version);
    // The server version is read while the menu is open, so the copy carries it
    await waitFor(() => expect(fetch).toHaveBeenCalled());
  });

  it("copies the version details and confirms with a Toast", async () => {
    open();
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));

    const version = screen.getByRole("menuitem", { name: new RegExp(`^${PRODUCT_NAME} 1\\.4\\.8`) });
    fireEvent.click(version);

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(`${PRODUCT_NAME} 1.4.8\nWeb: 1.4.8 (en-US build)\nServer: 1.4.8`);
    await waitFor(() => expect(screen.getByTestId("toasts").textContent).toBe("Version details copied"));
  });

  it("falls back to a single-line prompt when the clipboard is refused", async () => {
    const prompt = vi.spyOn(window, "prompt").mockReturnValue(null);
    open();
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    writeText.mockRejectedValue(new Error("denied"));

    fireEvent.click(screen.getByRole("menuitem", { name: new RegExp(`^${PRODUCT_NAME} 1\\.4\\.8`) }));

    await waitFor(() =>
      expect(prompt).toHaveBeenCalledWith(
        "Copy the version details below:",
        `${PRODUCT_NAME} 1.4.8 · Web: 1.4.8 (en-US build) · Server: 1.4.8`,
      ),
    );
    expect(screen.getByTestId("toasts").textContent).toBe("");
    prompt.mockRestore();
  });
});

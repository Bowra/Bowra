"use client";

/**
 * Interface-language menu (Header, Language & Paging Spec §1, D1–D3). One selection menu serves the account
 * menu's "Language ›" submenu and the Settings › Account "Interface language" menu button: "Automatic" (follows
 * the browser) on two lines, a separator, then 中文 / English / Deutsch / Français in their own names and a fixed
 * order, with a leading check on the saved choice. Choosing a language changes only the interface — never the
 * answer language (`output_language`), which has its own setting — and reloads the page once after the saved
 * `ui_language` is written (at most two seconds). The public pages keep their own cookie-only menu
 * (components/public-shell.tsx) and share the same name table (lib/locale-names.ts), without "Automatic".
 */

import { useCallback, useId, useRef, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ChevronDown, Languages } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { Spinner } from "@/components/icons";
import { MENU_DESCRIPTION_ATTR, MenuFloating, MenuSubmenu, useMenu, useMenuItem } from "@/components/ui/menu";
import { setUserSetting, setWorkspaceSetting } from "@/lib/api";
import { browserLocale, type Locale } from "@/lib/locale";
import { LOCALE_NATIVE_NAME, LOCALE_ORDER, clearLocaleCookie, readLocaleCookie, writeLocaleCookie } from "@/lib/locale-names";
import { getActiveSpace } from "@/lib/use-space";
import type { UiLanguagePreference } from "@/lib/use-language-preferences";

/** Maximum language-write wait: never trap users in switching if the network hangs. */
export const SETTING_WRITE_TIMEOUT_MS = 2000;
const PROFILE_STORAGE_KEY = "bowhead.user_profile";

function rememberProfileLanguage(next: UiLanguagePreference) {
  try {
    const raw = window.localStorage.getItem(PROFILE_STORAGE_KEY);
    const profile = raw ? JSON.parse(raw) : {};
    profile.language = next;
    window.localStorage.setItem(PROFILE_STORAGE_KEY, JSON.stringify(profile));
    window.dispatchEvent(new CustomEvent("bowhead:profile-changed"));
  } catch {
    /* Storage unavailable: the cookie and the saved setting still carry the choice. */
  }
}

/** The language a preference shows right now ("Automatic" resolves to the browser language). */
export function resolvedInterfaceLocale(preference: UiLanguagePreference): Locale {
  return preference === "auto" ? browserLocale() : preference;
}

/**
 * Switch the interface language: write the cookie (or clear it for "Automatic"), await saving `ui_language`
 * for at most {@link SETTING_WRITE_TIMEOUT_MS}, then reload once (D3). Choosing the saved option only closes
 * the menu. `beforeSwitch` may cancel, e.g. to confirm discarding unsaved edits on the settings page.
 */
export function useInterfaceLanguageSwitch({
  saved,
  beforeSwitch,
  onSaved,
  reload = () => window.location.reload(),
}: {
  /** The saved preference; drives the check and the "already current" test. */
  saved: UiLanguagePreference;
  beforeSwitch?: () => Promise<boolean> | boolean;
  onSaved?: (next: UiLanguagePreference) => void;
  reload?: () => void;
}) {
  const [switching, setSwitching] = useState<UiLanguagePreference | null>(null);
  const busyRef = useRef(false);

  const switchTo = useCallback(
    async (next: UiLanguagePreference) => {
      // "Automatic" with a leftover NEXT_LOCALE cookie is not yet in effect: save it, clear the cookie and reload.
      const pendingAuto = next === "auto" && readLocaleCookie() !== null;
      if (busyRef.current || (next === saved && !pendingAuto)) return;
      if (beforeSwitch && !(await beforeSwitch())) return;
      busyRef.current = true;
      setSwitching(next);
      if (next === "auto") clearLocaleCookie();
      else writeLocaleCookie(next);
      rememberProfileLanguage(next);
      // ★ Await saving before reload: the Gateway's `/me` writes the persisted `ui_language` back into
      // `NEXT_LOCALE` (and deletes it for "auto"). A reload racing an unsaved PUT would restore the old value.
      // Interface and AI-answer preferences are independent: this never writes output_language.
      await Promise.race([
        Promise.allSettled([setUserSetting("ui_language", next)]),
        new Promise((resolve) => setTimeout(resolve, SETTING_WRITE_TIMEOUT_MS)),
      ]);
      const wsId = getActiveSpace();
      if (wsId) void setWorkspaceSetting(wsId, "i18n_v1", "true").catch(() => {});
      onSaved?.(next);
      reload();
    },
    [beforeSwitch, onSaved, reload, saved],
  );

  return { switching, switchTo };
}

/** Option rows of the language menu, rendered inside a MenuFloating (account submenu or settings button menu). */
export function LanguageMenuOptions({
  saved,
  onPick,
}: {
  saved: UiLanguagePreference;
  onPick: (next: UiLanguagePreference) => void;
}) {
  return (
    <>
      <AutoOption checked={saved === "auto"} onPick={onPick} />
      <div role="separator" className="ui-row-menu-sep" />
      {LOCALE_ORDER.map((option) => (
        <LocaleOption key={option} locale={option} checked={saved === option} onPick={onPick} />
      ))}
    </>
  );
}

function AutoOption({ checked, onPick }: { checked: boolean; onPick: (next: UiLanguagePreference) => void }) {
  const t = useTranslations("locale");
  const now = LOCALE_NATIVE_NAME[browserLocale()];
  const itemProps = useMenuItem({
    role: "menuitemradio",
    checked,
    label: t("auto"),
    onSelect: () => onPick("auto"),
  });
  return (
    <button type="button" className="app-floating-menu-item ui-language-option" data-language-option="auto" {...itemProps}>
      <span className="ui-language-option__text">
        <span>{t("auto")}</span>
        <small className="ui-language-option__hint" {...{ [MENU_DESCRIPTION_ATTR]: "" }}>
          {/* `script` lets Chinese copy put a space before a Latin-script name ("当前为 English", "当前为中文"). */}
          {t("auto_hint", { language: now, script: /^[A-Za-zÀ-ž]/.test(now) ? "latin" : "other" })}
        </small>
      </span>
    </button>
  );
}

function LocaleOption({
  locale,
  checked,
  onPick,
}: {
  locale: Locale;
  checked: boolean;
  onPick: (next: UiLanguagePreference) => void;
}) {
  const itemProps = useMenuItem({
    role: "menuitemradio",
    checked,
    label: LOCALE_NATIVE_NAME[locale],
    onSelect: () => onPick(locale),
  });
  return (
    <button type="button" className="app-floating-menu-item" lang={locale} data-language-option={locale} {...itemProps}>
      <span>{LOCALE_NATIVE_NAME[locale]}</span>
    </button>
  );
}

/**
 * Account-menu entry (D1, Menus Spec §§2–3): "Language" with the current value and ›, a submenu of the language
 * options, and a last link row to the answer-language setting (not a radio option, no check column).
 * Narrow bottom sheets drill down inside the same sheet (MenuSubmenu).
 */
export function LanguageMenuEntry({
  saved,
  onPick,
  answerLink,
}: {
  saved: UiLanguagePreference;
  onPick: (next: UiLanguagePreference) => void;
  /** The trailing "Answer language settings" row; omitted where there is no account. */
  answerLink?: ReactNode;
}) {
  const sidebarT = useTranslations("sidebar");
  const locale = useLocale() as Locale;
  const selectedIndex = saved === "auto" ? 0 : LOCALE_ORDER.indexOf(saved) + 1;
  return (
    <MenuSubmenu
      label={sidebarT("language")}
      className="app-floating-menu-item v2-user-menu-item"
      icon={
        <span className="v2-user-menu-icon" aria-hidden="true">
          <LucideGlyph icon={Languages} size={16} aria-hidden="true" />
        </span>
      }
      value={LOCALE_NATIVE_NAME[locale] ?? locale}
      valueClassName="v2-user-menu-gate"
      menuClassName="app-floating-menu ui-submenu ui-language-menu"
      selectedIndex={selectedIndex}
    >
      <LanguageMenuOptions saved={saved} onPick={onPick} />
      {answerLink ? (
        <>
          <div role="separator" className="ui-row-menu-sep" />
          {answerLink}
        </>
      ) : null}
    </MenuSubmenu>
  );
}

/** Link row to Settings › Account › Answer language, with the current answer language at the end. */
export function AnswerLanguageLinkItem({ value, onSelect, href }: { value: string; href: string; onSelect: () => void }) {
  const sidebarT = useTranslations("sidebar");
  const itemProps = useMenuItem({ onSelect: (event) => { event.preventDefault(); onSelect(); }, label: sidebarT("answer_language_link") });
  return (
    <a href={href} className="app-floating-menu-item ui-language-answer-link" {...itemProps}>
      <span>{sidebarT("answer_language_link")}</span>
      <span className="ui-menu-item-tail v2-user-menu-gate">{value}</span>
    </a>
  );
}

/** Screen-reader announcement and visible text while a switch is pending ("Switching to English…"). */
export function useSwitchingLabel(switching: UiLanguagePreference | null): string | null {
  const t = useTranslations("locale");
  if (!switching) return null;
  return t("switching", { language: LOCALE_NATIVE_NAME[resolvedInterfaceLocale(switching)] });
}

/**
 * Settings › Account trigger (D1, §1b): a menu button showing the current interface language; while switching it
 * shows a small ring and "Switching to …" (it may widen; the page is not disabled).
 */
export function LanguageMenuButton({
  saved,
  beforeSwitch,
  onSaved,
  labelledBy,
}: {
  saved: UiLanguagePreference;
  beforeSwitch?: () => Promise<boolean> | boolean;
  onSaved?: (next: UiLanguagePreference) => void;
  /** Id of the row label naming this control. */
  labelledBy?: string;
}) {
  const t = useTranslations("topbar");
  const localeT = useTranslations("locale");
  const locale = useLocale() as Locale;
  const [open, setOpen] = useState(false);
  const { switching, switchTo } = useInterfaceLanguageSwitch({ saved, beforeSwitch, onSaved });
  const switchingLabel = useSwitchingLabel(switching);
  const selectedIndex = saved === "auto" ? 0 : LOCALE_ORDER.indexOf(saved) + 1;
  const menu = useMenu({
    open,
    onOpenChange: setOpen,
    placement: "bottom-start",
    offset: 4,
    selectedIndex,
    sheetTitle: localeT("menu_title"),
  });
  const valueId = useId();
  const current = LOCALE_NATIVE_NAME[locale] ?? locale;
  return (
    <>
      <button
        type="button"
        ref={menu.refs.setReference}
        className="ui-filter-menu-button ui-language-button"
        data-testid="interface-language-button"
        aria-labelledby={labelledBy ? `${labelledBy} ${valueId}` : undefined}
        aria-label={labelledBy ? undefined : t("switch_language_current", { locale: current })}
        data-switching={switching ? "true" : undefined}
        {...menu.getReferenceProps()}
      >
        {switching ? <Spinner aria-hidden="true" className="ui-language-button__spinner" /> : null}
        <span id={valueId} className="ui-filter-menu-button__value">
          {switchingLabel ?? current}
        </span>
        {switching ? null : (
          <LucideGlyph icon={ChevronDown} size={14} aria-hidden="true" className="ui-filter-menu-button__chevron" />
        )}
      </button>
      <span role="status" aria-live="polite" className="unified-work-sr-only">
        {switchingLabel ?? ""}
      </span>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          data-testid="interface-language-menu"
          {...menu.getFloatingProps({ className: "ui-filter-menu ui-language-menu", "aria-label": localeT("menu_title") })}
        >
          <LanguageMenuOptions saved={saved} onPick={(next) => void switchTo(next)} />
        </div>
      </MenuFloating>
    </>
  );
}

"use client";

/**
 * Settings › Account › "Answer language" (Header, Language & Paging Spec D2, §1b). Writes the existing
 * `output_language` account setting: "Match my question" saves `auto` (unset, follows the question), a language
 * saves that locale. The interface language is a separate setting and is never touched here.
 *
 * - Options list only allowed values: Deutsch appears only while the deployment's German-answer switch is on
 *   (`/me` capability `german_ai_output`), or when it is already the saved value.
 * - A team lock disables the button (aria-disabled, still focusable) and keeps the reason on a second line.
 * - Changes save immediately with a Toast; no reload, no save bar.
 */

import { useId, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ChevronDown } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { MENU_DESCRIPTION_ATTR, MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import type { Locale } from "@/lib/locale";
import { LOCALE_NATIVE_NAME, LOCALE_ORDER, isLocale } from "@/lib/locale-names";
import {
  answerFollowsQuestion,
  type AnswerLanguageChoice,
  useLanguagePreferences,
} from "@/lib/use-language-preferences";
import { toast } from "@/lib/use-toast";

/** Display name of a stored or locked answer-language value. */
export function useAnswerLanguageName(): (value: string) => string {
  const t = useTranslations("locale");
  const locale = useLocale() as Locale;
  return (value: string) => {
    if (isLocale(value)) return LOCALE_NATIVE_NAME[value];
    // A team lock of "auto" pins answers to the interface language.
    if (value === "auto_interface") return LOCALE_NATIVE_NAME[locale] ?? locale;
    return t("answer_follow");
  };
}

/** The answer language that applies to this account: the team lock when present, otherwise the saved value. */
export function effectiveAnswerValue(output: string, teamLock: string | null): string {
  if (teamLock === null) return output;
  return teamLock === "auto" ? "auto_interface" : teamLock;
}

function answerOptions(output: string, germanEnabled: boolean): AnswerLanguageChoice[] {
  return [
    "auto",
    ...LOCALE_ORDER.filter((locale) => locale !== "de-DE" || germanEnabled || output === "de-DE"),
  ];
}

function AnswerOption({
  value,
  checked,
  onPick,
}: {
  value: AnswerLanguageChoice;
  checked: boolean;
  onPick: (value: AnswerLanguageChoice) => void;
}) {
  const t = useTranslations("locale");
  const label = value === "auto" ? t("answer_follow_menu") : LOCALE_NATIVE_NAME[value];
  const itemProps = useMenuItem({ role: "menuitemradio", checked, label, onSelect: () => onPick(value) });
  return (
    <button
      type="button"
      className="app-floating-menu-item ui-language-option"
      lang={value === "auto" ? undefined : value}
      data-answer-option={value}
      {...itemProps}
    >
      {value === "auto" ? (
        <span className="ui-language-option__text">
          <span>{label}</span>
          <small className="ui-language-option__hint" {...{ [MENU_DESCRIPTION_ATTR]: "" }}>
            {t("answer_follow_hint")}
          </small>
        </span>
      ) : (
        <span>{label}</span>
      )}
    </button>
  );
}

export function AnswerLanguageMenuButton({ labelledBy }: { labelledBy?: string }) {
  const t = useTranslations("locale");
  const session = useOptionalAuthSession();
  const germanEnabled = Boolean(session?.me?.capabilities?.german_ai_output);
  const prefs = useLanguagePreferences();
  const nameOf = useAnswerLanguageName();
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const reasonId = useId();
  const valueId = useId();
  const locked = prefs.teamLock !== null;
  const ready = prefs.status === "ready";
  const options = answerOptions(prefs.output, germanEnabled);
  const followsQuestion = answerFollowsQuestion(prefs.output);
  const selectedIndex = followsQuestion ? 0 : Math.max(0, options.indexOf(prefs.output as Locale));
  const menu = useMenu({
    open: open && !locked && ready && !saving,
    // A locked, loading or saving control does not open (aria-disabled keeps it focusable for the reason).
    onOpenChange: (next) => setOpen(next && !locked && ready && !saving),
    placement: "bottom-start",
    offset: 4,
    selectedIndex,
    sheetTitle: t("answer_menu_title"),
  });
  const shown = nameOf(effectiveAnswerValue(prefs.output, prefs.teamLock));

  const pick = async (value: AnswerLanguageChoice) => {
    // "Match my question" is already chosen for auto, unset and the older explicit follow_input; keep it as is.
    if ((value === "auto" && followsQuestion) || value === prefs.output) return;
    setSaving(true);
    try {
      await prefs.saveAnswerLanguage(value);
      toast({ kind: "success", text: t("answer_saved") });
    } catch {
      toast({ kind: "error", text: t("answer_save_failed") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <span className="ui-answer-language">
      <button
        type="button"
        ref={menu.refs.setReference}
        className="ui-filter-menu-button ui-language-button"
        data-testid="answer-language-button"
        aria-labelledby={labelledBy ? `${labelledBy} ${valueId}` : undefined}
        aria-describedby={locked ? reasonId : undefined}
        aria-disabled={locked || !ready || saving ? true : undefined}
        aria-busy={saving || undefined}
        {...menu.getReferenceProps()}
      >
        <span id={valueId} className="ui-filter-menu-button__value">
          {ready ? shown : "…"}
        </span>
        <LucideGlyph icon={ChevronDown} size={14} aria-hidden="true" className="ui-filter-menu-button__chevron" />
      </button>
      {locked ? (
        <span id={reasonId} className="ui-answer-language__reason" data-testid="answer-language-locked">
          {t("answer_locked", { language: shown })}
        </span>
      ) : null}
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          data-testid="answer-language-menu"
          {...menu.getFloatingProps({ className: "ui-filter-menu ui-language-menu", "aria-label": t("answer_menu_title") })}
        >
          {options.map((value) => (
            <AnswerOption
              key={value}
              value={value}
              checked={value === "auto" ? followsQuestion : value === prefs.output}
              onPick={(next) => void pick(next)}
            />
          ))}
        </div>
      </MenuFloating>
    </span>
  );
}

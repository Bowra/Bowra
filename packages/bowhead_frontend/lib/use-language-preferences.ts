"use client";

/**
 * Account language preferences shared by the account menu and Settings › Account (Header, Language & Paging
 * Spec D1–D2). Interface language (`ui_language`) and answer language (`output_language`) are independent
 * settings: choosing one never writes the other. A missing `ui_language` or `"auto"` follows the browser; a
 * missing `output_language`, `"auto"` and the older explicit `"follow_input"` all answer in the question's
 * language. A team lock (`language_policy.locks.output_language`) overrides the account's answer language.
 *
 * One module-level snapshot serves every mounted reader, so the menu's "Answer language settings" value and
 * the settings row stay in step after a save.
 */

import { useCallback, useEffect, useSyncExternalStore } from "react";

import { getUserSettings, setUserSetting } from "./api";
import { apiJson } from "./api-client";
import type { Locale } from "./locale";
import { isLocale, readLocaleCookie } from "./locale-names";

export type UiLanguagePreference = "auto" | Locale;

/** Answer-language values offered by the settings row: follow the question, or a concrete language. */
export type AnswerLanguageChoice = "auto" | Locale;

export interface LanguagePreferencesSnapshot {
  status: "idle" | "loading" | "ready" | "error";
  ui: UiLanguagePreference;
  /** Stored answer preference as saved ("auto", "follow_input" or a locale); "auto" when unset. */
  output: string;
  /** The team-locked answer value, or null when the team does not lock it. */
  teamLock: string | null;
}

const INITIAL: LanguagePreferencesSnapshot = { status: "idle", ui: "auto", output: "auto", teamLock: null };

let snapshot: LanguagePreferencesSnapshot = INITIAL;
let inflight: Promise<void> | null = null;
const listeners = new Set<() => void>();

function publish(next: LanguagePreferencesSnapshot) {
  snapshot = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Read the team lock; any failure (no team, older gateway) means "not locked", the safe reading for display. */
async function fetchTeamLock(): Promise<string | null> {
  try {
    const body = await apiJson<{ value: string | null }>("/api/v1/team-settings/language-policy");
    if (!body.value) return null;
    const policy = JSON.parse(body.value) as {
      defaults?: { output_language?: unknown };
      locks?: { output_language?: unknown };
    };
    const value = policy.defaults?.output_language;
    return policy.locks?.output_language && typeof value === "string" ? value : null;
  } catch {
    return null;
  }
}

function load(): Promise<void> {
  if (inflight) return inflight;
  publish({ ...snapshot, status: snapshot.status === "ready" ? "ready" : "loading" });
  inflight = Promise.all([getUserSettings(), fetchTeamLock()])
    .then(([settings, teamLock]) => {
      const ui = settings.ui_language;
      const output = settings.output_language;
      publish({
        status: "ready",
        // Unset (not "auto"): older accounts switched by cookie only, so the cookie is the effective choice.
        ui: isLocale(ui) ? ui : ui === "auto" ? "auto" : (readLocaleCookie() ?? "auto"),
        output: typeof output === "string" && output ? output : "auto",
        teamLock,
      });
    })
    .catch(() => publish({ ...snapshot, status: "error" }))
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/** Whether a stored answer preference means "answer in the question's language". */
export function answerFollowsQuestion(value: string): boolean {
  return value === "auto" || value === "follow_input" || !isLocale(value);
}

/**
 * Load (once per page) and read the account's language preferences.
 * `enabled=false` defers the request, e.g. until the account menu is first opened.
 */
export function useLanguagePreferences(enabled = true) {
  const current = useSyncExternalStore(subscribe, () => snapshot, () => INITIAL);
  useEffect(() => {
    if (enabled && snapshot.status === "idle") void load();
  }, [enabled]);

  const saveAnswerLanguage = useCallback(async (choice: AnswerLanguageChoice) => {
    await setUserSetting("output_language", choice);
    publish({ ...snapshot, output: choice });
  }, []);

  const rememberUiLanguage = useCallback((ui: UiLanguagePreference) => {
    publish({ ...snapshot, ui });
  }, []);

  return { ...current, reload: load, saveAnswerLanguage, rememberUiLanguage };
}

/** Test seam: reset the shared snapshot between tests. */
export function resetLanguagePreferencesForTests() {
  inflight = null;
  publish(INITIAL);
}

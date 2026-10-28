"use client";

import { useCallback, useEffect, useState } from "react";

import { getUserSettings, setUserSetting } from "./api";

/** What happens after Coach writes the draft: check it automatically (default) or wait for the Owner. */
export type CoachAfterEdit = "check" | "ask";

export const COACH_AFTER_EDIT_KEY = "agent_coach_after_edit";

export function coachAfterEdit(value: unknown): CoachAfterEdit {
  return value === "ask" ? "ask" : "check";
}

/**
 * The per-account setting shared by the canvas "⋯" menu and Profile. `failed` reports a save the
 * server did not confirm; the shown value then stays at the last confirmed one.
 */
export function useCoachAfterEdit(): {
  value: CoachAfterEdit | null;
  saving: boolean;
  failed: boolean;
  change: (next: CoachAfterEdit) => Promise<void>;
} {
  const [value, setValue] = useState<CoachAfterEdit | null>(null);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void getUserSettings()
      .then((settings) => { if (!cancelled) setValue(coachAfterEdit(settings[COACH_AFTER_EDIT_KEY])); })
      .catch(() => { if (!cancelled) setValue("check"); });
    return () => { cancelled = true; };
  }, []);
  const change = useCallback(async (next: CoachAfterEdit) => {
    setSaving(true);
    setFailed(false);
    try {
      await setUserSetting(COACH_AFTER_EDIT_KEY, next);
      setValue(next);
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
    }
  }, []);
  return { value, saving, failed, change };
}

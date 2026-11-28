"use client";

/**
 * How the running view draws its work (chat running state R3.1, R3.2): 列表 (default), 胶囊, 泳道 or 时间轴.
 *
 * One account setting, `running_view_style`, written by the Appearance page and by the switcher on the running card.
 * A style picked on the card becomes the reader's default from then on, in every conversation, browser and device.
 * The browser keeps a copy only to draw the first frame; the account value always wins once it is read, and the
 * copy is never pushed to the account. A missing value means the list.
 *
 * Every running card and trace reads the hook, so the account value is fetched once per page and shared.
 */
import { useCallback, useEffect, useState } from "react";

import { deleteUserSetting, getUserSettings, setUserSetting } from "./api";

export const RUNNING_VIEW_STYLES = ["list", "chips", "lanes", "timeline"] as const;
export type RunningViewStyle = (typeof RUNNING_VIEW_STYLES)[number];
export const DEFAULT_RUNNING_VIEW_STYLE: RunningViewStyle = "list";

export const RUNNING_VIEW_STYLE_KEY = "running_view_style";
const LOCAL_KEY = "bowhead.running_view_style";
const EVENT_NAME = "bowhead:running-view-style-changed";

export function isRunningViewStyle(value: unknown): value is RunningViewStyle {
  return typeof value === "string" && (RUNNING_VIEW_STYLES as readonly string[]).includes(value);
}

function readLocal(): RunningViewStyle {
  if (typeof window === "undefined") return DEFAULT_RUNNING_VIEW_STYLE;
  try {
    const value = window.localStorage.getItem(LOCAL_KEY);
    return isRunningViewStyle(value) ? value : DEFAULT_RUNNING_VIEW_STYLE;
  } catch {
    return DEFAULT_RUNNING_VIEW_STYLE;
  }
}

function writeLocal(style: RunningViewStyle): void {
  if (typeof window === "undefined") return;
  try {
    if (style === DEFAULT_RUNNING_VIEW_STYLE) window.localStorage.removeItem(LOCAL_KEY);
    else window.localStorage.setItem(LOCAL_KEY, style);
  } catch {
    /* Private mode or quota: the account value still applies after it is read. */
  }
  window.dispatchEvent(new CustomEvent(EVENT_NAME));
}

let accountRead: Promise<void> | null = null;

/** Read the account value once per page; it replaces the local copy. */
function readAccountOnce(): Promise<void> {
  if (accountRead) return accountRead;
  try {
    accountRead = getUserSettings()
      .then((settings) => {
        const value = settings[RUNNING_VIEW_STYLE_KEY];
        writeLocal(isRunningViewStyle(value) ? value : DEFAULT_RUNNING_VIEW_STYLE);
      })
      .catch(() => {
        // Unreadable now: keep the local copy and try again on the next page.
        accountRead = null;
      });
  } catch {
    // The running card mounts in many views; an unavailable settings client must not take the view down.
    accountRead = null;
    return Promise.resolve();
  }
  return accountRead;
}

/** Read the account value again: the Configuration Agent changed the run view (chat running state R3.6). */
export function refreshRunningViewStyle(): Promise<void> {
  accountRead = null;
  return readAccountOnce();
}

/** Test-only: forget the shared account read. */
export function resetRunningViewStyleForTests(): void {
  accountRead = null;
}

/**
 * Save a style on the account. The view switches at once; when the save fails it switches back and the promise
 * resolves false, so the caller can say why.
 */
export async function saveRunningViewStyle(next: RunningViewStyle): Promise<boolean> {
  const previous = readLocal();
  writeLocal(next);
  try {
    if (next === DEFAULT_RUNNING_VIEW_STYLE) await deleteUserSetting(RUNNING_VIEW_STYLE_KEY);
    else await setUserSetting(RUNNING_VIEW_STYLE_KEY, next);
    return true;
  } catch {
    writeLocal(previous);
    return false;
  }
}

export function useRunningViewStyle(): {
  style: RunningViewStyle;
  setStyle: (next: RunningViewStyle) => Promise<boolean>;
} {
  const [style, setLocalStyle] = useState<RunningViewStyle>(() => readLocal());

  useEffect(() => {
    const onChange = () => setLocalStyle(readLocal());
    const onStorage = (event: StorageEvent) => {
      if (event.key === LOCAL_KEY || event.key === null) onChange();
    };
    window.addEventListener(EVENT_NAME, onChange);
    window.addEventListener("storage", onStorage);
    void readAccountOnce();
    return () => {
      window.removeEventListener(EVENT_NAME, onChange);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const setStyle = useCallback((next: RunningViewStyle) => saveRunningViewStyle(next), []);
  return { style, setStyle };
}

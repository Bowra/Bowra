"use client";

/**
 * use-preferred-model · user's currently selected LLM (provider_id:model).
 *
 * Design document: docs/M157-llm-provider-registry-design.md §3.4
 *
 * Dual-source strategy (same as use-providers):
 *  - localStorage `bowhead.preferred_model` (immediate effect)
 *  - backend user_settings.preferred_model (cross-device synchronization)
 *
 * Composer picker reads/writes; M2 backend turns/start puts this field in routing context; agents use effective.
 *
 * Synchronization: the custom event within a tab and native storage events across tabs.
 */

import { useCallback, useEffect, useState } from "react";

import { getUserSettings, setUserSetting } from "./api";
import { unpackPreferredModel } from "./provider-registry";

const LS_KEY = "bowhead.preferred_model";
const EVENT_NAME = "bowhead:preferred-model-changed";

function _readLocal(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(LS_KEY);
  } catch {
    return null;
  }
}

function _writeLocal(v: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (v == null) window.localStorage.removeItem(LS_KEY);
    else window.localStorage.setItem(LS_KEY, v);
    window.dispatchEvent(new CustomEvent(EVENT_NAME));
  } catch { /* quota */ }
}

export function usePreferredModel() {
  const [preferred, setPreferred] = useState<string | null>(() => _readLocal());

  useEffect(() => {
    let cancelled = false;
    void getUserSettings().then((server) => {
      if (cancelled) return;
      const serverVal = server.preferred_model;
      if (typeof serverVal === "string" && serverVal.length > 0) {
        setPreferred(serverVal);
        _writeLocal(serverVal);
      } else if (preferred) {
        void setUserSetting("preferred_model", preferred).catch(() => {});
      }
    }).catch(() => {});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onChange = () => setPreferred(_readLocal());
    const onStorage = (event: StorageEvent) => {
      if (event.key === LS_KEY || event.key === null) onChange();
    };
    window.addEventListener(EVENT_NAME, onChange);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(EVENT_NAME, onChange);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const select = useCallback((providerId: string, model: string) => {
    const v = `${providerId}:${model}`;
    setPreferred(v);
    _writeLocal(v);
    void setUserSetting("preferred_model", v).catch(() => {});
  }, []);

  const clear = useCallback(() => {
    setPreferred(null);
    _writeLocal(null);
    // Keep backend deleteUserSetting usage simple; setting an empty string also works, so do that here to clear the backend value.
    void setUserSetting("preferred_model", "").catch(() => {});
  }, []);

  // Parse into a (providerId, model) tuple for the picker
  const parsed = preferred ? unpackPreferredModel(preferred) : null;

  return { preferred, parsed, select, clear };
}

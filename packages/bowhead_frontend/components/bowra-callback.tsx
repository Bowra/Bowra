"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { PublicShell } from "@/components/public-shell";
import { ApiError } from "@/lib/api-client";
import { appPath } from "@/lib/base-path";
import { completeBowraConnection, type BowraCallbackResult } from "@/lib/bowra-connect-api";

export function BowraCallback() {
  const t = useTranslations("settings.models");
  const started = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Strict Mode must not submit the one-use authorization twice.
    if (started.current) return;
    started.current = true;
    const query = new URLSearchParams(window.location.search);
    window.history.replaceState(null, "", window.location.pathname);
    const state = query.get("state");
    const code = query.get("code");
    const failure = query.get("error");
    if (
      !state ||
      ["state", "code", "error"].some((key) => query.getAll(key).length > 1) ||
      query.has("code") === query.has("error") ||
      (query.has("code") && !code) ||
      (query.has("error") && !failure)
    ) {
      setError("invalid_callback");
      return;
    }
    const result: BowraCallbackResult = { state, ...(code !== null ? { code } : { error: failure! }) };
    void completeBowraConnection(result)
      .then((outcome) => {
        window.location.replace(`${appPath("/team/settings/models/")}?${new URLSearchParams(outcome)}`);
      })
      .catch((cause: unknown) => {
        setError(cause instanceof ApiError && [401, 402, 403].includes(cause.status) ? "permission_changed" : "exchange_uncertain");
      });
  }, []);

  return (
    <PublicShell title={t("overview.use_bowra")}>
      {error ? (
        <>
          <p role="alert">{t(`bowra.${error}`)}</p>
          <a className="btn sec" href={appPath("/team/settings/models/")}>
            {t("bowra.return_to_models")}
          </a>
        </>
      ) : (
        <p role="status">{t("bowra.connecting")}</p>
      )}
    </PublicShell>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { AppShell } from "@/components/shell/AppShell";
import { Topbar } from "@/components/shell/Topbar";
import { administrationDestinationsForUser } from "@/lib/navigation-centers";
import { fetchMe } from "@/lib/auth-api";
import { trackInteraction as track } from "@/lib/metrics";
import { useAppRouter } from "@/lib/use-app-router";

type EntryState = "loading" | "denied" | "error";

/**
 * `/admin/` is a compatibility entry, not a second administration product.
 *
 * The standalone IA names a management-console destination but does not define
 * a new dashboard. Route to the first existing destination the current role can
 * actually open, while preserving every leaf URL for deep links and bookmarks.
 */
export function AdministrationEntry() {
  const router = useAppRouter();
  const t = useTranslations("administration_hub");
  const tNav = useTranslations("sidebar");
  const [state, setState] = useState<EntryState>("loading");
  const [retryTick, setRetryTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    void fetchMe()
      .then((user) => {
        if (cancelled) return;
        const destination = administrationDestinationsForUser(user)[0];
        if (!destination) {
          setState("denied");
          return;
        }
        track("bowhead_admin_destination_opened_total", {
          destination: destination.id,
          surface: "admin_entry",
        });
        router.replace(destination.route);
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [retryTick, router]);

  const title = tNav("user_menu.administration");

  return (
    <AppShell
      mobileTitle={title}
      topbar={<Topbar titleSlot={<span className="title">{title}</span>} />}
    >
      <div className="administration-entry">
        {state === "loading" ? (
          <p role="status">{t("loading")}</p>
        ) : null}
        {state === "denied" ? (
          <p role="alert">{t("access_denied")}</p>
        ) : null}
        {state === "error" ? (
          <div>
            <p role="alert">{t("load_failed")}</p>
            <button
              type="button"
              className="settings-inline-retry"
              onClick={() => setRetryTick((current) => current + 1)}
            >
              {t("retry")}
            </button>
          </div>
        ) : null}
      </div>
    </AppShell>
  );
}

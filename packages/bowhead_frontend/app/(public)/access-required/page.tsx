"use client";

import {
 useEffect, useState } from "react";

import { AuthShell } from "@/components/auth-shell";
import { UsersIcon,
  useBrandedTranslations,
} from "@/components/public-shell";
import { fetchMe, logoutUser } from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";

export default function AccessRequiredPage() {
  const t = useBrandedTranslations("auth.access_required");
  const [email, setEmail] = useState("");

  useEffect(() => {
    void fetchMe()
      .then((user) => {
        if (!user.membership_required) {
          window.location.replace(appPath("/"));
          return;
        }
        setEmail(user.email);
      })
      .catch(() => window.location.replace(appPath("/login/")));
  }, []);

  return (
    <AuthShell
      title={t("title")}
      subtitle={t("subtitle")}
      status={{ tone: "neutral", icon: <UsersIcon /> }}
    >
      {email ? <p className="pub-note">{t("current_account", { email })}</p> : null}
      <div className="pub-actions">
        <a href={appPath("/invite/")} className="pub-button pub-button--primary">
          {t("open_invite")}
        </a>
        <button
          type="button"
          className="pub-button pub-button--ghost"
          onClick={() => {
            void logoutUser().finally(() => window.location.replace(appPath("/login/")));
          }}
        >
          {t("switch_account")}
        </button>
      </div>
    </AuthShell>
  );
}

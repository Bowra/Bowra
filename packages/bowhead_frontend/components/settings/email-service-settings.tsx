"use client";

import { useTranslations } from "next-intl";

import { SettingsEmailSection } from "./email-config";
import { withUiHooks } from "./ui-hook-classes";
import rawStyles from "./email-service.module.css";

// Append a global ui-email-* hook to each class (new appearance in styles/ui-refresh-pages-settings.css).
const styles = withUiHooks(rawStyles, "ui-email");

export function EmailServiceSettings() {
  const t = useTranslations("settings.integrations");

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className="ui-page-title">{t("email_title")}</h1>
        <p className="ui-page-desc">{t("email_subtitle")}</p>
      </header>
      <SettingsEmailSection />
    </div>
  );
}

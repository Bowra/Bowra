"use client";

import { useTranslations } from "next-intl";

export function SettingsExport() {
  const t = useTranslations("settings.export");

  return (
    <div>
      <h1 className="ui-page-title legacy-export-title">
        {t("title")}
      </h1>
      <p className="ui-page-desc legacy-export-desc">{t("subtitle")}</p>

      <section className="legacy-export-section ui-group">
        <h3 className="legacy-export-heading ui-group-title">{t("tpl_section")}</h3>
        <p className="legacy-export-hint ui-group-desc">{t("tpl_hint")}</p>
      </section>

      <section className="ui-group">
        <h3 className="legacy-export-heading ui-group-title">{t("method_section")}</h3>
        <p className="legacy-export-method ui-group-desc">{t("method_hint")}</p>
      </section>
    </div>
  );
}

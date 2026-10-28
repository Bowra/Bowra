"use client";

/**
 * Sprint β G10 — OAuth-only user info card (per v9.2 G-3 fix).
 *
 * Shown when user.has_password === false (OAuth-only signup).
 * Tells user no local password to change + link to provider.
 */

import { useTranslations } from "next-intl";
import { CharIcon } from "@/components/char-icon";


const PROVIDER_LINKS: Record<string, { labelKey: "oauth_link_feishu"; url: string }> = {
  feishu: {
    labelKey: "oauth_link_feishu",
    url: "https://www.feishu.cn/admin/personalProfile",
  },
};


interface Props {
  providers: string[];
}


export function OAuthOnlyState({ providers }: Props) {
  const t = useTranslations("profile.security");
  const primary = providers[0];
  const link = primary ? PROVIDER_LINKS[primary] : null;

  return (
    <div className="oauth-only-card">
      <div className="oauth-only-card__icon"><CharIcon ch="🔒" /></div>
      <h3 className="oauth-only-card__title">{t("oauth_title")}</h3>
      <p className="oauth-only-card__body">
        {t("oauth_body_prefix")}
        {providers.length > 0 ? (
          <strong>{providers.join(" / ")}</strong>
        ) : (
          <strong>{t("oauth_provider_fallback")}</strong>
        )}
        {t("oauth_body_middle")}
        {link ? (
          <>
            {" "}
            <a href={link.url} target="_blank" rel="noopener noreferrer">
              {t(link.labelKey)}
            </a>
            {t("oauth_body_sentence_end")}
          </>
        ) : (
          t("oauth_body_provider_center")
        )}
      </p>
    </div>
  );
}

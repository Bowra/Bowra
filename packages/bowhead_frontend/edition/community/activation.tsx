"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  AuthError,
  AuthField,
  AuthShell,
  PasswordInput,
  PasswordRules,
  passwordMeetsRules,
} from "@/components/auth-shell";
import { BusyLabel } from "@/components/public-shell";
import { getBootstrapStatus, postAuthJson } from "@/lib/auth-api";
import { PRODUCT_NAME } from "@/lib/product-identity";
import { useAppRouter } from "@/lib/use-app-router";

export default function CommunitySetupPage() {
  const t = useTranslations("community_setup");
  const auth = useTranslations("auth");
  const router = useAppRouter();
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    void getBootstrapStatus()
      .then((status) => {
        if (!active) return;
        if (!status.required) router.replace("/login/");
        else setReady(true);
      })
      .catch(() => {
        if (active) setError(t("status_failed"));
      });
    return () => {
      active = false;
    };
  }, [router, t]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || !ready) return;
    if (!passwordMeetsRules(password)) {
      setError(auth("field_error.password_rules"));
      return;
    }
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      await postAuthJson("/api/v1/bootstrap/community", {
        setup_token: String(data.get("setup_token") ?? "").trim(),
        team_name: String(data.get("team_name") ?? "").trim(),
        display_name: String(data.get("display_name") ?? "").trim() || undefined,
        email: String(data.get("email") ?? "")
          .trim()
          .toLowerCase(),
        password,
      });
      router.replace("/login/");
    } catch (cause) {
      const code = cause && typeof cause === "object" && "code" in cause ? String(cause.code) : "";
      setError(
        t(
          code === "invalid_setup_token"
            ? "invalid_token"
            : code === "setup_unavailable"
              ? "already_initialized"
              : "failed",
        ),
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthShell title={t("title", { product: PRODUCT_NAME })} subtitle={t("subtitle")}>
      <form className="pub-form" onSubmit={submit}>
        <p>{t("token_hint")}</p>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          <code>python -m bowhead_gateway.installation setup-token</code>
        </pre>
        <div className="pub-fields">
          <AuthField label={t("token")}>
            {(control) => (
              <input
                {...control}
                name="setup_token"
                type="password"
                required
                autoComplete="off"
                spellCheck={false}
                disabled={!ready || busy}
                className="pub-input"
              />
            )}
          </AuthField>
          <AuthField label={t("team_name")}>
            {(control) => (
              <input
                {...control}
                name="team_name"
                required
                maxLength={80}
                disabled={!ready || busy}
                className="pub-input"
              />
            )}
          </AuthField>
          <AuthField label={t("display_name")} aside={auth("field.optional")}>
            {(control) => (
              <input
                {...control}
                name="display_name"
                autoComplete="name"
                disabled={!ready || busy}
                className="pub-input"
              />
            )}
          </AuthField>
          <AuthField label={t("email")}>
            {(control) => (
              <input
                {...control}
                name="email"
                type="email"
                required
                autoComplete="email"
                disabled={!ready || busy}
                className="pub-input"
              />
            )}
          </AuthField>
          <AuthField label={t("password")} rules={<PasswordRules value={password} />}>
            {(control) => (
              <PasswordInput
                control={control}
                required
                autoComplete="new-password"
                value={password}
                onChange={setPassword}
                readOnly={!ready || busy}
                showLabel={auth("field.show_password")}
                hideLabel={auth("field.hide_password")}
              />
            )}
          </AuthField>
        </div>
        <AuthError message={error} />
        <button
          type="submit"
          disabled={!ready || busy}
          aria-busy={busy || undefined}
          className="pub-button pub-button--primary"
        >
          <BusyLabel busy={busy} idle={t("submit")} pending={t("submitting")} />
        </button>
      </form>
    </AuthShell>
  );
}

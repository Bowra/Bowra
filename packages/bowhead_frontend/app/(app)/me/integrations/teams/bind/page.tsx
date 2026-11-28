"use client";

import { Suspense, useState } from "react";
import { useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";

import { AppShell } from "@/components/shell/AppShell";
import { apiJson, ApiError } from "@/lib/api-client";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";

import styles from "../../feishu/bind/bind.module.css";

export default function TeamsBindingPage() {
  return (
    <Suspense>
      <BindingForm />
    </Suspense>
  );
}

function BindingForm() {
  const t = useTranslations("settings.integrations.teams");
  const token = useSearchParams().get("pair") ?? "";
  const auth = useAuthRedirect();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<
    | "bind_success"
    | "bind_expired"
    | "bind_mismatch"
    | "bind_conflict"
    | "bind_error"
    | null
  >(null);

  async function confirm() {
    setBusy(true);
    try {
      await apiJson("/api/v1/integrations/teams/bindings/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      setResult("bind_success");
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0;
      setResult(
        status === 400
          ? "bind_expired"
          : status === 403
            ? "bind_mismatch"
            : status === 409
              ? "bind_conflict"
              : "bind_error",
      );
    } finally {
      setBusy(false);
    }
  }

  const message = !token
    ? "bind_expired"
    : (result ?? (auth.status === "error" ? "bind_error" : "bind_intro"));
  return (
    <AppShell mobileTitle={t("bind_title")}>
      <section className={styles.stateBody}>
        <h1>{t("bind_title")}</h1>
        <p role="status">{t(message)}</p>
        {auth.me && <p>{auth.me.email}</p>}
        {token &&
          auth.status === "authed" &&
          (!result || result === "bind_error") && (
            <button
              type="button"
              className={styles.primaryButton}
              disabled={busy}
              onClick={confirm}
            >
              {t("bind_confirm")}
            </button>
          )}
      </section>
    </AppShell>
  );
}

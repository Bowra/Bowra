"use client";

import { useId, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Notice } from "@/components/ui/notice";
import { ApiError, apiJson } from "@/lib/api-client";
import { clearInvalidSessionAndRedirect, type MeUser } from "@/lib/auth-api";

const ERROR_KEYS = {
  account_transfer_required: "transfer",
  account_space_transfer_required: "spaces",
  account_resource_transfer_required: "resources",
  account_email_confirmation_required: "email_error",
  account_password_confirmation_failed: "password_error",
  account_recent_login_required: "login_error",
  account_credentials_changed: "login_error",
  account_deletion_not_ready: "unavailable",
  account_memory_cleanup_unavailable: "unavailable",
  account_erasure_backend_unavailable: "storage_error",
  account_erasure_unreviewed_table: "schema_error",
  account_erasure_unreviewed_column: "schema_error",
  account_erasure_schema_incomplete: "schema_error",
  account_erasure_unreviewed_secondary_schema: "schema_error",
  account_erasure_secondary_missing: "schema_error",
} as const;

export function AccountDeletionControl({ user }: { user: MeUser }) {
  const t = useTranslations("settings.profile.account_deletion");
  const descriptionId = useId();
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    if (busy) return;
    setOpen(false);
    setEmail("");
    setPassword("");
    setError(null);
  };
  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiJson("/api/v1/profile", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirm_email: email,
          password: user.has_password ? password : undefined,
        }),
      });
      setPassword("");
      await clearInvalidSessionAndRedirect("/");
    } catch (caught) {
      const code = caught instanceof ApiError ? caught.code : undefined;
      const key =
        code && code in ERROR_KEYS
          ? ERROR_KEYS[code as keyof typeof ERROR_KEYS]
          : "failed";
      const body =
        caught instanceof ApiError
          ? (caught.body as
              { detail?: { teams?: { name?: unknown }[] } } | undefined)
          : undefined;
      const teams = Array.isArray(body?.detail?.teams)
        ? body.detail.teams
            .map((team) => team?.name)
            .filter((name): name is string => typeof name === "string")
        : [];
      setError(
        `${t(key)}${teams.length ? ` ${t("affected", { teams: teams.join(", ") })}` : ""}`,
      );
      setBusy(false);
    }
  };
  return (
    <section aria-label={t("title")}>
      <h2 className="ui-section-title">{t("title")}</h2>
      <p className="ui-page-desc">{t("summary")}</p>
      <Button variant="danger" onClick={() => setOpen(true)}>
        {t("open")}
      </Button>
      {open ? (
        <Dialog
          title={t("title")}
          describedBy={descriptionId}
          onClose={close}
          busy={busy}
          footer={
            <>
              <Button variant="secondary" onClick={close} disabled={busy}>
                {t("cancel")}
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  void submit();
                }}
                disabled={
                  busy ||
                  email.trim().toLowerCase() !==
                    user.email.trim().toLowerCase() ||
                  (user.has_password && !password)
                }
              >
                {t(busy ? "deleting" : "confirm")}
              </Button>
            </>
          }
        >
          <p id={descriptionId}>{t("consequences")}</p>
          <label>
            {t("email")}
            <input
              className="ui-input"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              disabled={busy}
            />
          </label>
          {user.has_password ? (
            <label>
              {t("password")}
              <input
                className="ui-input"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={busy}
              />
            </label>
          ) : (
            <p>{t("recent_login")}</p>
          )}
          {error ? <Notice tone="error">{error}</Notice> : null}
        </Dialog>
      ) : null}
    </section>
  );
}

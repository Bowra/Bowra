"use client";

import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Field, fieldDescribedBy } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { publicBaseUrl } from "@/lib/base-path";
import {
  createPublicEntry,
  deletePublicEntry,
  disablePublicEntry,
  fetchPublicEntries,
  reissuePublicEntryChallenge,
  setDefaultPublicEntry,
  type PublicEntryDTO,
} from "@/lib/api";

import { withUiHooks } from "./ui-hook-classes";
import rawStyles from "./public-entries.module.css";

import { copyTextWithFallback } from "@/lib/clipboard";

// Append global ui-pe-* hooks to every class (styles/ui-refresh-pages-settings.css).
const styles = withUiHooks(rawStyles, "ui-pe");

interface VerificationChallenge {
  entryId: string;
  url: string;
  expiresAt: string | null;
}

function isValidPublicBaseUrl(value: string): boolean {
  const raw = value.trim();
  if (!raw || raw.includes("\\") || !/^[\x20-\x7E]+$/.test(raw)) return false;
  try {
    const parsed = new URL(raw);
    return (
      ["http:", "https:"].includes(parsed.protocol) &&
      Boolean(parsed.hostname) &&
      !parsed.username &&
      !parsed.password &&
      !parsed.search &&
      !parsed.hash &&
      !parsed.hostname.endsWith(".") &&
      !parsed.hostname.includes("*") &&
      !parsed.pathname.includes("//")
    );
  } catch {
    return false;
  }
}

export function SettingsPublicEntries() {
  const t = useTranslations("settings.public_entries");
  const baseUrlId = useId();
  const [entries, setEntries] = useState<PublicEntryDTO[]>([]);
  const [baseUrl, setBaseUrl] = useState("");
  const [verification, setVerification] =
    useState<VerificationChallenge | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedValue, setCopiedValue] = useState<string | null>(null);
  const [disableTarget, setDisableTarget] = useState<PublicEntryDTO | null>(
    null,
  );
  const [replacementId, setReplacementId] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<PublicEntryDTO | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchPublicEntries()
      .then((result) => {
        if (cancelled) return;
        setEntries(result);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshEntries = useCallback(async () => {
    const result = await fetchPublicEntries();
    setEntries(result);
    setVerification((current) =>
      current &&
      result.some(
        (entry) => entry.id === current.entryId && entry.status === "active",
      )
        ? null
        : current,
    );
  }, []);

  useEffect(() => {
    if (!verification) return;
    const onFocus = () => {
      void refreshEntries().catch(() => undefined);
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refreshEntries, verification]);

  const run = async (operation: () => Promise<void>): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      await operation();
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const baseUrlValid = isValidPublicBaseUrl(baseUrl);
  const baseUrlError =
    baseUrl.trim() && !baseUrlValid ? t("base_url_invalid") : undefined;
  const currentEntry = entries.find(
    (entry) => entry.is_current && entry.status === "active",
  );
  // The address this page is actually being served at, so an owner can compare it
  // against the registered ones without leaving the page.
  const unmatchedBaseUrl = currentEntry ? null : publicBaseUrl();
  const activeReplacements = useMemo(
    () =>
      entries.filter(
        (entry) => entry.status === "active" && entry.id !== disableTarget?.id,
      ),
    [disableTarget?.id, entries],
  );

  const addEntry = () =>
    run(async () => {
      const result = await createPublicEntry(baseUrl.trim());
      setBaseUrl("");
      setVerification({
        entryId: result.entry.id,
        url: result.verification_url,
        expiresAt: result.expires_at,
      });
      setEntries((current) => [
        result.entry,
        ...current.filter((entry) => entry.id !== result.entry.id),
      ]);
    });

  const makeDefault = (entryId: string) =>
    run(async () => {
      const updated = await setDefaultPublicEntry(entryId);
      setEntries((current) =>
        current.map((candidate) => ({
          ...candidate,
          is_default: candidate.id === updated.id,
        })),
      );
    });
  const reissue = (entryId: string) =>
    run(async () => {
      const result = await reissuePublicEntryChallenge(entryId);
      setVerification({
        entryId: result.entry.id,
        url: result.verification_url,
        expiresAt: result.expires_at,
      });
      setEntries((current) =>
        current.map((entry) =>
          entry.id === result.entry.id
            ? {
                ...entry,
                ...result.entry,
                is_current: entry.is_current,
                match_source: entry.match_source,
              }
            : entry,
        ),
      );
    });

  const requestDisable = (entry: PublicEntryDTO) => {
    setDisableTarget(entry);
    setReplacementId("");
  };

  const confirmDisable = async () => {
    if (!disableTarget) return;
    const disableLast = activeReplacements.length === 0;
    const succeeded = await run(async () => {
      const updated = await disablePublicEntry(disableTarget.id, {
        new_default_entry_id: disableTarget.is_default
          ? replacementId || undefined
          : undefined,
        disable_last: disableLast,
      });
      setEntries((current) =>
        current.map((entry) => {
          if (entry.id === updated.id) {
            return {
              ...entry,
              ...updated,
              is_current: entry.is_current,
              match_source: entry.match_source,
            };
          }
          if (entry.id === replacementId) {
            return { ...entry, is_default: true };
          }
          return entry;
        }),
      );
    });
    if (succeeded) {
      setDisableTarget(null);
      setReplacementId("");
    }
  };

  const remove = async () => {
    if (!deleteTarget) return;
    const succeeded = await run(async () => {
      await deletePublicEntry(deleteTarget.id);
      setEntries((current) =>
        current.filter((entry) => entry.id !== deleteTarget.id),
      );
    });
    if (succeeded) setDeleteTarget(null);
  };

  const copyValue = async (key: string, value: string) => {
    if (!(await copyTextWithFallback(value, t("copy_failed")))) {
      setError(t("copy_failed"));
      return;
    }
    setCopiedValue(key);
  };

  const disableNeedsReplacement = Boolean(
    disableTarget?.is_default && activeReplacements.length > 0,
  );
  const canConfirmDisable = !disableNeedsReplacement || Boolean(replacementId);

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className="ui-page-title">{t("title")}</h1>
        <p className="ui-page-desc">{t("description")}</p>
      </header>

      {error ? <Notice tone="error">{error}</Notice> : null}
      <form
        className={styles.addForm}
        onSubmit={(event) => {
          event.preventDefault();
          if (baseUrlValid) void addEntry();
        }}
      >
          <Field
            id={baseUrlId}
            label={t("base_url")}
            hint={t("base_url_hint")}
            error={baseUrlError}
          >
            <div className={styles.addControls} data-testid="public-entry-add-controls">
              <input
                id={baseUrlId}
                className="settings-input"
                type="url"
                inputMode="url"
                autoComplete="url"
                placeholder="https://bowhead.example.com/team-a"
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                disabled={busy}
                aria-invalid={Boolean(baseUrlError)}
                aria-describedby={fieldDescribedBy(baseUrlId, {
                  hint: true,
                  error: Boolean(baseUrlError),
                })}
              />
              <Button
                type="submit"
                variant="primary"
                disabled={busy || !baseUrlValid}
              >
                {t("add")}
              </Button>
            </div>
          </Field>
      </form>

      <Notice tone="muted">{t("lifecycle_hint")}</Notice>

      {verification ? (
        <section className={styles.verification}>
          <strong>{t("verify_title")}</strong>
          <p>{t("verify_description")}</p>
          <p>{t("verify_direct_hint")}</p>
          {verification.expiresAt ? (
            <p>
              {t("verify_expires", {
                expiresAt: new Date(verification.expiresAt).toLocaleString(),
              })}
            </p>
          ) : null}
          {verification.url.startsWith("http://") ? (
            <p className={styles.warningText} role="note">
              {t("verify_http_warning")}
            </p>
          ) : null}
          <a href={verification.url} target="_blank" rel="noreferrer">
            {verification.url}
          </a>
          <div className={styles.verificationActions}>
            <Button
              size="sm"
              onClick={() => void copyValue("verification", verification.url)}
            >
              {copiedValue === "verification" ? t("copied") : t("copy")}
            </Button>
            <Button
              size="sm"
              disabled={busy}
              onClick={() => void run(refreshEntries)}
            >
              {t("refresh_verification")}
            </Button>
          </div>
        </section>
      ) : null}

      {entries.some((entry) => entry.status === "active") ? (
        currentEntry ? (
          <div className={styles.currentSummary}>
            <span>{t("current_request")}</span>
            <strong>{currentEntry.base_url}</strong>
            <small>
              {t("current_match", {
                source: t(
                  `match_source.${currentEntry.match_source ?? "unknown"}`,
                ),
              })}
            </small>
          </div>
        ) : (
          // Show the unmatched address so administrators can identify host
          // spelling differences that prevent public-entry branding.
          <Notice tone="info">
            <span>{t("current_unresolved")}</span>
            {unmatchedBaseUrl ? (
              <strong className={styles.unmatchedAddress}>{unmatchedBaseUrl}</strong>
            ) : null}
          </Notice>
        )
      ) : null}

      <div className={styles.cards}>
        {entries.length === 0 ? (
          <p className={styles.empty}>{t("empty")}</p>
        ) : null}
        {entries.map((entry) => (
          <article
            key={entry.id}
            className={`${styles.card} ${entry.is_current ? styles.currentCard : ""}`}
          >
            <div className={styles.cardHeader}>
              <div className={styles.entryIdentity}>
                <div>{entry.base_url}</div>
                <div className={styles.entryId}>
                  <code>{entry.id}</code>
                  <Button
                    size="sm"
                    onClick={() =>
                      void copyValue(`entry:${entry.id}`, entry.id)
                    }
                  >
                    {copiedValue === `entry:${entry.id}`
                      ? t("copied")
                      : t("copy_id")}
                  </Button>
                </div>
              </div>
              <div className={styles.badges}>
                {entry.is_current ? (
                  <span className={`${styles.badge} ${styles.currentBadge}`}>
                    {t("current")}
                  </span>
                ) : null}
                <span className={styles.badge}>
                  {t(`status.${entry.status}`)}
                </span>
                {entry.is_default ? (
                  <span className={styles.badge}>{t("default")}</span>
                ) : null}
                <RowActionsMenu
                  label={t("more_actions_for", { url: entry.base_url })}
                  pending={busy}
                  actions={[
                    ...(entry.status === "pending_verification" || entry.status === "disabled"
                      ? [{ id: "reissue", label: t("reissue"), disabled: busy, onSelect: () => void reissue(entry.id) }]
                      : []),
                    ...(entry.status === "active" && !entry.is_default
                      ? [{ id: "make-default", label: t("make_default"), disabled: busy, onSelect: () => void makeDefault(entry.id) }]
                      : []),
                    entry.status === "active"
                      ? {
                          id: "disable",
                          label: t("disable"),
                          danger: true,
                          disabled: busy,
                          onSelect: () => requestDisable(entry),
                        }
                      : {
                          id: "delete",
                          label: t("delete"),
                          danger: true,
                          disabled: busy,
                          onSelect: () => setDeleteTarget(entry),
                        },
                  ]}
                />
              </div>
            </div>

            {entry.base_url.startsWith("http://") ? (
              <div className={styles.httpWarning} role="note">
                <strong>{t("http_warning_title")}</strong>
                <span>{t("http_warning")}</span>
              </div>
            ) : null}

          </article>
        ))}
      </div>

      {disableTarget ? (
        <Dialog
          title={t("disable_dialog_title")}
          onClose={() => {
            setDisableTarget(null);
            setReplacementId("");
          }}
          busy={busy}
          footer={
            <>
              <Button
                disabled={busy}
                onClick={() => {
                  setDisableTarget(null);
                  setReplacementId("");
                }}
              >
                {t("cancel")}
              </Button>
              <Button
                variant="danger"
                disabled={busy || !canConfirmDisable}
                onClick={() => void confirmDisable()}
              >
                {t("confirm_disable_button")}
              </Button>
            </>
          }
        >
          <p className={styles.dialogText}>
            {activeReplacements.length === 0
              ? t("confirm_disable_last")
              : t("confirm_disable", { url: disableTarget.base_url })}
          </p>
          {disableNeedsReplacement ? (
            <Field
              id="public-entry-replacement"
              label={t("replacement_label")}
              hint={t("replacement_hint")}
            >
              <select
                id="public-entry-replacement"
                className="settings-select"
                value={replacementId}
                onChange={(event) => setReplacementId(event.target.value)}
                disabled={busy}
              >
                <option value="">{t("replacement_placeholder")}</option>
                {activeReplacements.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.base_url}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
        </Dialog>
      ) : null}

      {deleteTarget ? (
        <ConfirmDialog
          title={t("delete_dialog_title")}
          description={t("confirm_delete", { url: deleteTarget.base_url })}
          confirmLabel={t("confirm_delete_button")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => void remove()}
        />
      ) : null}

    </div>
  );
}

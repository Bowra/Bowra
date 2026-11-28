"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";
import { ApiError, apiJson } from "@/lib/api-client";
import { fmtSize } from "@/lib/files-taxonomy";

const endpoint = "/api/v1/team-settings/file-retention";
type Policy = { days: number | null; effective_days: number; version: number };
type Review = { review_id: string; impact: Record<string, { count: number; bytes: number }> };
function Editor() {
  const t = useTranslations("file_library");
  const [saved, setSaved] = useState<Policy | null>(null);
  const [choice, setChoice] = useState("inherit");
  const [review, setReview] = useState<Review | null>(null);
  const [existing, setExisting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const ctrl = new AbortController();
    setSaved(null);
    setReview(null);
    setError("");
    apiJson<Policy>(endpoint, { cache: "no-store", signal: ctrl.signal })
      .then((p) => {
        if (ctrl.signal.aborted) return;
        setSaved(p);
        setChoice(p.days === null ? "inherit" : String(p.days));
      })
      .catch(() => {
        if (!ctrl.signal.aborted) setError(t("policy_error"));
      });
    return () => ctrl.abort();
  }, [reload, t]);
  async function submit(apply: boolean) {
    if (!saved || busy || (apply && !review)) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const init = {
        method: apply ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          apply
            ? { review_id: review!.review_id, include_existing: existing }
            : { expected_version: saved.version, days: choice === "inherit" ? null : Number(choice) },
        ),
      };
      if (apply) {
        setSaved(await apiJson<Policy>(endpoint, init));
        setReview(null);
        setExisting(false);
        setMessage(t("saved"));
      } else {
        setReview(await apiJson<Review>(endpoint + "/review", init));
        setExisting(false);
      }
    } catch (err) {
      setReview(null);
      const status = err instanceof ApiError ? err.status : 0;
      setError(t(status === 409 || status === 404 ? "review_stale" : status === 403 ? "admin_only" : "review_error"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="file-policy">
      <h1>{t("policy")}</h1>
      {error && (
        <p role="alert">
          {error} <button onClick={() => setReload((n) => n + 1)}>{t("reload")}</button>
        </p>
      )}
      {saved && (
        <>
          <p>
            {t("effective", {
              period: saved.effective_days ? t("days", { n: saved.effective_days }) : t("off"),
              source: t(saved.days === null ? "inherit" : "team"),
            })}
          </p>
          <label>
            {t("period")}{" "}
            <select
              value={choice}
              disabled={busy}
              onChange={(e) => {
                setChoice(e.target.value);
                setReview(null);
                setMessage("");
              }}
            >
              {["inherit", "30", "90", "0"].map((v) => (
                <option key={v} value={v}>
                  {v === "inherit" ? t("inherit") : v === "0" ? t("off") : t("days", { n: v })}
                </option>
              ))}
            </select>
          </label>
          <button className="btn sec" disabled={busy} onClick={() => void submit(false)}>
            {t("review")}
          </button>
          {review && (
            <section>
              <h2>{t("impact")}</h2>
              <p>{t("scope_explanation")}</p>
              <dl>
                {Object.entries({ eligible: { count: 0, bytes: 0 }, ...review.impact }).map(([reason, stat]) => (
                  <div key={reason}>
                    <dt>{t("reason." + reason)}</dt>
                    <dd>
                      {t("file_count", { n: stat.count })} · {fmtSize(stat.bytes)}
                    </dd>
                  </div>
                ))}
              </dl>
              <label>
                <input
                  type="checkbox"
                  disabled={busy}
                  checked={existing}
                  onChange={(e) => setExisting(e.target.checked)}
                />
                {t("historical")}
              </label>
              <button className="btn pri" disabled={busy} onClick={() => void submit(true)}>
                {t("apply")}
              </button>
            </section>
          )}
        </>
      )}
      {message && <p role="status">{message}</p>}
    </section>
  );
}
export default function FileRetentionPage() {
  return (
    <TeamSettingsRoute active="file-retention">
      <Editor />
    </TeamSettingsRoute>
  );
}

"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { getTeamOperations, saveTeamOperationsTimezone, type OperationsDay, type TeamOperationsReport } from "@/lib/team-operations-api";
import styles from "./team-operations.module.css";

type Range = 1 | 7 | 30;
type Metric = "login_users" | "sessions" | "tokens" | "background_tokens" | "skill_loads" | "tool_loads" | "tool_calls";

const metrics: Metric[] = ["login_users", "sessions", "tokens", "background_tokens", "skill_loads", "tool_loads", "tool_calls"];
const zones = ["Asia/Shanghai", "UTC", "Asia/Tokyo", "Europe/Berlin", "Europe/London", "America/New_York", "America/Los_Angeles"];

function value(row: OperationsDay, metric: Metric): number {
  return metric === "tokens" ? row.input_tokens + row.output_tokens : row[metric];
}

export function TeamOperationsContent() {
  const t = useTranslations("settings.teamOperations");
  const [range, setRange] = useState<Range>(7);
  const [metric, setMetric] = useState<Metric>("login_users");
  const [report, setReport] = useState<TeamOperationsReport | null>(null);
  const [timezone, setTimezone] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    getTeamOperations(range).then((data) => {
      if (!cancelled) {
        setReport(data);
        setTimezone(data.timezone);
      }
    }).catch(() => { if (!cancelled) setError(true); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [range, refresh]);

  const totals = useMemo(() => {
    const values = Object.fromEntries(metrics.map((key) => [key, 0])) as Record<Metric, number>;
    for (const row of report?.days ?? []) for (const key of metrics) values[key] += value(row, key);
    return values;
  }, [report]);
  const maximum = Math.max(1, ...(report?.days ?? []).map((row) => value(row, metric)));

  async function saveTimezone() {
    setSaving(true);
    setSaveMessage("");
    try {
      await saveTeamOperationsTimezone(timezone);
      setSaveMessage(t("timezone_saved"));
      setRefresh((n) => n + 1);
    } catch {
      setSaveMessage(t("save_error"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <main className={styles.page}>
      <h1 className="ui-page-title">{t("title")}</h1>
      <p>{t("subtitle")}</p>
      <div className={styles.controls}>
        <div className={styles.ranges} role="group" aria-label={t("title")}>
          {([1, 7, 30] as const).map((days) => (
            <button key={days} type="button" className="ui-btn" aria-pressed={range === days} onClick={() => setRange(days)}>
              {t(days === 1 ? "yesterday" : days === 7 ? "last7" : "last30")}
            </button>
          ))}
        </div>
        <label className={styles.timezone}>
          {t("timezone")}
          <input list="team-operations-zones" value={timezone} onChange={(event) => { setTimezone(event.target.value); setSaveMessage(""); }} />
          <datalist id="team-operations-zones">{zones.map((zone) => <option key={zone} value={zone} />)}</datalist>
        </label>
        <button type="button" className="ui-btn" onClick={() => void saveTimezone()} disabled={saving || !timezone || timezone === report?.timezone}>
          {t("save_timezone")}
        </button>
      </div>
      {saveMessage ? <p role="status">{saveMessage}</p> : null}
      {loading ? <p role="status">{t("loading")}</p> : error ? (
        <p role="alert">{t("load_error")} <button type="button" className="ui-btn" onClick={() => setRefresh((n) => n + 1)}>{t("retry")}</button></p>
      ) : report ? (
        <>
          <h2>{t("range_total")}</h2>
          <div className={styles.summary}>
            {metrics.map((key) => <div className={styles.summaryItem} key={key}><span>{t(key)}</span><strong>{totals[key].toLocaleString()}</strong></div>)}
        </div>
          {range > 1 ? (
            <section>
              <div className={styles.trendHead}>
                <h2>{t("trend")}</h2>
                <select aria-label={t("trend")} value={metric} onChange={(event) => setMetric(event.target.value as Metric)}>
                  {metrics.map((key) => <option value={key} key={key}>{t(key)}</option>)}
                </select>
              </div>
              <div className={styles.chart} role="img" aria-label={`${t("trend")}: ${t(metric)}`}>
                {report.days.map((row) => <div className={styles.barGroup} key={row.day} title={`${row.day}: ${value(row, metric).toLocaleString()}`}>
                  <span className={styles.bar} style={{ height: `${value(row, metric) / maximum * 100}%` }} />
                  <small>{row.day.slice(5)}</small>
                </div>)}
              </div>
            </section>
          ) : null}
          <section>
            <h2>{t("daily_table")}</h2>
            <div className={styles.tableScroll}>
              <table className={styles.table}>
                <thead><tr><th>{t("day")}</th>{metrics.map((key) => <th key={key}>{t(key)}</th>)}</tr></thead>
                <tbody>{report.days.map((row) => <tr key={row.day}><th>{row.day}</th>{metrics.map((key) => <td key={key}>{value(row, key).toLocaleString()}</td>)}</tr>)}</tbody>
              </table>
            </div>
          </section>
        </>
      ) : null}
    </main>
  );
}

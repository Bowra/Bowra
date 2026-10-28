"use client";

/**
 * LedgerTable—single-user ledger details (drill-down).
 *
 * Columns: Time / Type / Workspace / Tokens / Δ⚡
 * Filter: ledger type / All
 * Paginated, default size=50.
 *
 * Do **not link** turn_id to /admin/turns/{id} (privacy boundary, PRD §2).
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { ListRow, ListRows, useListRowsLayout } from "@/components/ui/list-rows";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { fetchUserLedger, type LedgerEntry } from "@/lib/admin-credits-api";
import { adminCreditsErrorKey } from "@/lib/admin-credits-error";
import { CharIcon } from "@/components/char-icon";

interface Props {
  userId: string;
  refreshTick?: number;
}

const PAGE_SIZE = 50;

export function LedgerTable({ userId, refreshTick = 0 }: Props) {
  const t = useTranslations("admin.credits");
  const [items, setItems] = useState<LedgerEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [type, setType] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Switch tables to list rows at phone width (< 640; decided tradeoff 12).
  const listLayout = useListRowsLayout();

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchUserLedger(userId, {
      page, size: PAGE_SIZE,
      type: type || undefined,
    })
      .then((d) => {
        if (cancelled) return;
        setItems(d.items);
        setTotal(d.total);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(t(adminCreditsErrorKey(e)));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [userId, page, type, refreshTick, t]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <section className="legacy-credits-card">
      <div className="legacy-credits-ledger-head">
        <h2 className="legacy-credits-section-title legacy-credits-ledger-title">{t("ledger_table_title")}</h2>
        <select
          aria-label={t("ledger_type_filter")}
          value={type}
          onChange={(e) => { setType(e.target.value); setPage(1); }}
          className="ui-input legacy-credits-ledger-filter"
        >
          <option value="">{t("ledger_type_all")}</option>
          <option value="turn">{t("ledger_type_turn")}</option>
          <option value="subagent_draft_reservation">{t("ledger_type_subagent_draft_reservation")}</option>
          <option value="subagent_draft_reservation_refund">{t("ledger_type_subagent_draft_reservation_refund")}</option>
          <option value="subagent_draft_reservation_extra">{t("ledger_type_subagent_draft_reservation_extra")}</option>
          <option value="subagent_draft_reservation_release">{t("ledger_type_subagent_draft_reservation_release")}</option>
          <option value="admin_grant">{t("ledger_type_admin_grant")}</option>
          <option value="admin_set_balance">{t("ledger_type_admin_set_balance")}</option>
          <option value="period_reset">{t("ledger_type_period_reset")}</option>
        </select>
      </div>

      {listLayout ? (
        <div className="ui-table-frame legacy-credits-ledger-frame">
          {loading ? (
            <LoadingSkeleton rows={4} label={t("loading")} />
          ) : error ? (
            <div role="alert" className="legacy-credits-state legacy-credits-state--error">{error}</div>
          ) : items.length === 0 ? (
            <div className="legacy-credits-state legacy-credits-state--empty">{t("empty_ledger")}</div>
          ) : (
            <ListRows label={t("ledger_table_title")} className="credits-ledger-rows">
              {items.map((it) => <LedgerListRow key={it.id} entry={it} />)}
            </ListRows>
          )}
        </div>
      ) : (
      <div className="ui-table-frame legacy-credits-ledger-frame">
        <table className="ui-table legacy-credits-ledger-table">
          <thead>
            <tr>
              <Th>{t("ledger_col_time")}</Th>
              <Th>{t("ledger_col_type")}</Th>
              <Th>{t("ledger_col_space")}</Th>
              <Th>{t("ledger_col_turn")}</Th>
              <Th>{t("ledger_col_model")}</Th>
              <Th align="right" num>{t("ledger_col_tokens")}</Th>
              <Th align="right" num>Δ<CharIcon ch="⚡" /></Th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} className="legacy-credits-state legacy-credits-state--loading">
                <LoadingSkeleton rows={4} label={t("loading")} />
              </td></tr>
            ) : error ? (
              <tr><td colSpan={7} className="legacy-credits-state legacy-credits-state--error">
                {error}
              </td></tr>
            ) : items.length === 0 ? (
              <tr><td colSpan={7} className="legacy-credits-state legacy-credits-state--empty">
                {t("empty_ledger")}
              </td></tr>
            ) : (
              items.map((it) => <LedgerRow key={it.id} entry={it} />)
            )}
          </tbody>
        </table>
      </div>
      )}

      {totalPages > 1 ? (
        <div className="ui-pager legacy-credits-pager">
          <PageBtn disabled={page <= 1} onClick={() => setPage(page - 1)}><CharIcon ch="‹" /></PageBtn>
          <span className="ui-pager-summary legacy-credits-pager-summary">
            {page} / {totalPages}
          </span>
          <PageBtn disabled={page >= totalPages} onClick={() => setPage(page + 1)}><CharIcon ch="›" /></PageBtn>
        </div>
      ) : null}
    </section>
  );
}

function tokensLabel(entry: LedgerEntry): string {
  if (entry.model_usage?.length) {
    const totals = entry.model_usage.reduce((sum, line) => ({
      input: sum.input + line.tokens_input,
      cached: sum.cached + line.tokens_cached,
      output: sum.output + line.tokens_output,
    }), { input: 0, cached: 0, output: 0 });
    return `${shorten(totals.input)}/${shorten(totals.cached)}/${shorten(totals.output)}`;
  }
  return (entry.tokens_input || entry.tokens_output)
    ? `${shorten(entry.tokens_input)}/${shorten(entry.tokens_output)}`
    : "—";
}

function ModelUsage({ entry }: { entry: LedgerEntry }) {
  const t = useTranslations("admin.credits");
  const lines = entry.model_usage || [];
  const names = [...new Set(lines.map((line) => line.model ? `${line.provider ? `${line.provider} / ` : ""}${line.model}` : t("ledger_model_unknown")))];
  if (!names.length) return <>—</>;
  return <details>
    <summary>{names.join(", ")}</summary>
    <div>{lines.map((line, index) => <div key={index}>
      {line.model || t("ledger_model_unknown")}: {line.tokens_input} × {line.rate_input}
      {" + "}{line.tokens_cached} × {line.rate_cached}{" + "}{line.tokens_output} × {line.rate_output}
    </div>)}</div>
  </details>;
}

function LedgerType({ type }: { type: string }) {
  const t = useTranslations("admin.credits");
  return (
    <span className={`legacy-credits-ledger-type ${typeTone(type)}`}>
      {t.has(`ledger_type_${type}`) ? t(`ledger_type_${type}`) : t("ledger_type_other")}
    </span>
  );
}

function LedgerDelta({ delta }: { delta: number }) {
  const isPositive = delta > 0;
  return <>{isPositive ? "+" : ""}{delta.toLocaleString()}</>;
}

/**
 * Phone list row (< 640): type-badge title, time second, space · conversation turn · tokens third,
 * quota delta at row end (same as the rightmost table column).
 */
function LedgerListRow({ entry }: { entry: LedgerEntry }) {
  return (
    <ListRow
      title={<LedgerType type={entry.type} />}
      subtitle={<span className="ui-tabular">{formatDate(entry.created_at)}</span>}
      meta={[
        entry.workspace_id ? entry.workspace_id.slice(0, 8) : "—",
        entry.turn_id_short ?? "—",
        [...new Set((entry.model_usage || []).map((line) => line.model).filter(Boolean))].join(", ") || "—",
        tokensLabel(entry),
      ].join(" · ")}
      status={
        <span className={`legacy-credits-ledger-delta ui-tabular${entry.delta > 0 ? " is-positive" : ""}`}>
          <LedgerDelta delta={entry.delta} />
        </span>
      }
    />
  );
}

function LedgerRow({ entry }: { entry: LedgerEntry }) {
  return (
    <tr className="legacy-credits-ledger-row">
      <td className="legacy-credits-ledger-td">
        <span className="legacy-credits-ledger-meta">
          {formatDate(entry.created_at)}
        </span>
      </td>
      <td className="legacy-credits-ledger-td">
        <LedgerType type={entry.type} />
      </td>
      <td className="legacy-credits-ledger-td">
        <span className="legacy-credits-ledger-meta">
          {entry.workspace_id ? entry.workspace_id.slice(0, 8) : "—"}
        </span>
      </td>
      <td className="legacy-credits-ledger-td">
        <span
          title={entry.turn_id_short ? `turn id (full): ${entry.turn_id_short}` : undefined}
          className="legacy-credits-ledger-meta"
        >
          {/* B14 privacy boundary: do not link /admin/turns/{id} */}
          {entry.turn_id_short ?? "—"}
        </span>
      </td>
      <td className="legacy-credits-ledger-td legacy-credits-ledger-td--right legacy-credits-ledger-meta ui-num">
        <ModelUsage entry={entry} />
      </td>
      <td className="legacy-credits-ledger-td legacy-credits-ledger-td--right legacy-credits-ledger-meta ui-num">
        {tokensLabel(entry)}
      </td>
      <td className={`legacy-credits-ledger-td legacy-credits-ledger-td--right legacy-credits-ledger-delta ui-num${entry.delta > 0 ? " is-positive" : ""}`}>
        <LedgerDelta delta={entry.delta} />
      </td>
    </tr>
  );
}

// Former typeColor: type-specific inline colors become tone modifiers (values in styles/ui-controls-legacy.css “逐页验收 B”).
function typeTone(type: string): string {
  switch (type) {
    case "turn": return "is-turn";
    case "admin_grant": return "is-grant";
    case "admin_set_balance": return "is-set";
    default: return "is-muted";
  }
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    return `${d.toISOString().slice(0, 16).replace("T", " ")}`;
  } catch {
    return iso;
  }
}

function shorten(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

// Original inline styles match the credit-user table; reuse its legacy classes (styles/ui-controls-legacy.css batch four).
function Th({ children, align = "left", num = false }: { children: React.ReactNode; align?: "left" | "right" | "center"; num?: boolean }) {
  return (
    <th className={`legacy-credits-th legacy-credits-th--${align}${num ? " ui-num" : ""}`}>{children}</th>
  );
}

function PageBtn({ disabled, onClick, children }: {
  disabled: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      type="button" disabled={disabled} onClick={onClick}
      className="ui-pager-btn legacy-credits-pagebtn"
    >{children}</button>
  );
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { AdminShell } from "@/components/admin-shell";
import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { AdminRouteState } from "@/components/shell/AdminRouteState";
import { AccessWhoCanHelpWhenEnabled } from "@/components/access/access-request-panel";
import { ListRow, ListRows, useListRowsLayout } from "@/components/ui/list-rows";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import {
  fetchAdminTurnSnapshot,
  fetchTurnEvents,
  fetchTurnIds,
  type MemoryPackSnapshotDTO,
  type TurnEventDTO,
} from "@/lib/api";

export default function TurnsPage() {
  const tT = useTranslations("admin_turns");
  const auth = useAuthRedirect({ requireTeamAdmin: true });
  const [ids, setIds] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [events, setEvents] = useState<TurnEventDTO[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [snapshot, setSnapshot] = useState<MemoryPackSnapshotDTO | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [snapshotErr, setSnapshotErr] = useState<ShownError | null>(null);
  const [err, setErr] = useState<ShownError | null>(null);
  const initialTurnIdRef = useRef<string | null>(null);
  const initialTurnLoadedRef = useRef(false);
  const loadRequestRef = useRef(0);
  // At mobile widths (< 640), replace the event table with list rows (decision 12)
  const listLayout = useListRowsLayout();

  useEffect(() => {
    // Read initial id from the query string; defer actual loading until authentication succeeds.
    const u = new URL(window.location.href);
    const init = u.searchParams.get("id");
    if (init) {
      initialTurnIdRef.current = init;
      setQ(init);
      setSelected(init);
    }
  }, []);

  useEffect(() => {
    if (auth.status !== "authed") return;
    fetchTurnIds(200).then(setIds).catch((e) => setErr(shownError(tT("list_failed_notice"), e)));
    // tT changes with language; fetch the list here only once when authentication becomes ready.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auth.status]);

  const load = useCallback(async (tid: string) => {
    const requestId = ++loadRequestRef.current;
    setErr(null);
    setSelected(tid);
    setEvents([]);
    setEventsLoading(true);
    setSnapshot(null);
    setSnapshotLoading(true);
    setSnapshotErr(null);

    // Events and the optional memory snapshot are independent.  Each can
    // render as soon as it resolves, but only while this remains the newest
    // selection, so a slow previous turn cannot overwrite the highlighted row.
    const eventsRequest = fetchTurnEvents(tid).then(
      (value) => {
        if (requestId !== loadRequestRef.current) return;
        setEvents(value);
        setEventsLoading(false);
      },
      (reason: unknown) => {
        if (requestId !== loadRequestRef.current) return;
        setErr(shownError(tT("events_failed_notice"), reason));
        setEventsLoading(false);
      },
    );
    // An accessible turn with no recorded snapshot returns an empty result.
    const snapshotRequest = fetchAdminTurnSnapshot(tid).then(
      (value) => {
        if (requestId !== loadRequestRef.current) return;
        setSnapshot(value);
        setSnapshotLoading(false);
      },
      (reason: unknown) => {
        if (requestId !== loadRequestRef.current) return;
        setSnapshotErr(shownError(tT("snapshot_failed_notice"), reason));
        setSnapshotLoading(false);
      },
    );
    await Promise.all([eventsRequest, snapshotRequest]);
  }, [tT]);

  useEffect(() => {
    if (auth.status !== "authed") return;
    if (initialTurnLoadedRef.current) return;
    const init = initialTurnIdRef.current;
    if (!init) return;
    initialTurnLoadedRef.current = true;
    void load(init);
  }, [auth.status, load]);

  const filtered = q ? ids.filter((i) => i.includes(q)) : ids;

  // 401 → useAuthRedirect bounced. Show placeholder while loading/redirecting.
  if (auth.status === "loading" || auth.status === "redirecting") {
    return (
      <AdminShell current="traces" mobileTitle={tT("title")}>
        <AdminRouteState title={tT("title")}>
          {tT("loading")}
        </AdminRouteState>
      </AdminShell>
    );
  }
  if (auth.status === "not_admin") {
    return (
      <AdminShell current="traces" mobileTitle={tT("title")}>
        <AdminRouteState title={tT("title")} tone="forbidden" aside={<AccessWhoCanHelpWhenEnabled scope="team" />}>
          {tT("not_authorized")}
        </AdminRouteState>
      </AdminShell>
    );
  }
  if (auth.status === "error") {
    return (
      <AdminShell current="traces" mobileTitle={tT("title")}>
        <AdminRouteState title={tT("title")} tone="error">
          {tT("load_failed")}
        </AdminRouteState>
        <ErrorDiagnosticsInline error={auth.errorCause} surface="route" />
      </AdminShell>
    );
  }

  return (
    <AdminShell current="traces" mobileTitle={tT("title")}>
      <div className="admin-config-page">
        <div className="admin-config-page__content">
          <h1 className="ui-page-title">{tT("title")}</h1>
          <p className="admin-config-page__lead ui-page-desc">
            {tT("lead")}
          </p>

          <div className="legacy-turns-toolbar">
            <input
              type="text"
              placeholder={tT("filter_placeholder")}
              value={q}
              onChange={(e) => setQ(e.target.value)}
              className="ui-input legacy-turns-filter"
            />
            <button
              type="button"
              className="btn-primary"
              disabled={!q}
              onClick={() => q && load(q)}
            >
              {tT("load_btn")}
            </button>
            {/* C15: one-click "View latest" — picks the freshest turn from the
                fetched ids so first-time visitors aren't stuck on a blank panel. */}
            <button
              type="button"
              className="btn-secondary"
              disabled={ids.length === 0}
              onClick={() => {
                if (ids.length > 0) {
                  setQ(ids[0]);
                  void load(ids[0]);
                }
              }}
            >
              {tT("view_latest_btn")}
            </button>
          </div>

          {err ? (
            <div className="approval-card legacy-turns-error">
              <strong>{tT("error_label")}</strong> {err.message}
              <ErrorDiagnosticsInline error={err.error} surface="route" />
            </div>
          ) : null}

          <div className="legacy-turns-layout">
            <div className="legacy-turns-ids">
              <div className="legacy-turns-ids-head">
                {tT("recent", { n: ids.length })}
              </div>
              {filtered.map((tid) => (
                <div
                  key={tid}
                  role="button"
                  tabIndex={0}
                  onClick={() => load(tid)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") load(tid);
                  }}
                  className={`legacy-turns-id${selected === tid ? " is-selected" : ""}`}
                >
                  {tid}
                </div>
              ))}
              {filtered.length === 0 ? (
                <div className="legacy-turns-muted">{tT("no_match")}</div>
              ) : null}
            </div>

            <div className="legacy-turns-events">
              {!selected ? (
                <div className="legacy-turns-hint">
                  {tT("select_hint")}
                </div>
              ) : listLayout ? (
                <>
                  {eventsLoading ? (
                    <LoadingSkeleton rows={4} label={tT("events_loading")} />
                  ) : events.length === 0 && !err ? (
                    <div className="legacy-turns-hint" data-testid="admin-replay-events-empty">{tT("events_empty")}</div>
                  ) : null}
                  <ListRows label={tT("events_list_aria")} className="turns-event-rows">
                    {events.map((e) => (
                      <ListRow
                        key={e.seq}
                        title={
                          <span className={`intent-badge ${badgeFor(e.event_type)}`}>
                            <span className="ib-dot" />
                            {e.event_type}
                          </span>
                        }
                        subtitle={<span className="ui-tabular">#{e.seq} · {e.occurred_at.slice(11, 19)}</span>}
                        detail={
                          <pre className="legacy-turns-payload">
                            {truncate(JSON.stringify(e.payload, null, 2), 400)}
                          </pre>
                        }
                      />
                    ))}
                  </ListRows>
                </>
              ) : (
                <table className="legacy-turns-table">
                  <thead>
                    <tr className="legacy-turns-head">
                      <th className="legacy-turns-th">#</th>
                      <th className="legacy-turns-th">{tT("col_event")}</th>
                      <th className="legacy-turns-th">{tT("col_time")}</th>
                      <th className="legacy-turns-th">{tT("col_payload")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {eventsLoading ? (
                      <tr>
                        <td
                          colSpan={4}
                          data-testid="admin-replay-events-loading"
                          className="legacy-turns-state"
                        >
                          <LoadingSkeleton rows={4} label={tT("events_loading")} />
                        </td>
                      </tr>
                    ) : events.length === 0 && !err ? (
                      <tr>
                        <td
                          colSpan={4}
                          data-testid="admin-replay-events-empty"
                          className="legacy-turns-state"
                        >
                          {tT("events_empty")}
                        </td>
                      </tr>
                    ) : null}
                    {events.map((e) => (
                      <tr key={e.seq} className="legacy-turns-row">
                        <td className="legacy-turns-td ui-tabular">{e.seq}</td>
                        <td className="legacy-turns-td">
                          <span className={`intent-badge ${badgeFor(e.event_type)}`}>
                            <span className="ib-dot" />
                            {e.event_type}
                          </span>
                        </td>
                        <td className="legacy-turns-td legacy-turns-td--time ui-tabular">
                          {e.occurred_at.slice(11, 19)}
                        </td>
                        <td className="legacy-turns-td">
                          <pre className="legacy-turns-payload">
                            {truncate(JSON.stringify(e.payload, null, 2), 400)}
                          </pre>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {/* V4 PRD-D-4-4: memory pack snapshot panel */}
          {selected ? (
            <MemoryPackSnapshotPanel
              snapshot={snapshot}
              loading={snapshotLoading}
              error={snapshotErr}
            />
          ) : null}
        </div>
      </div>
    </AdminShell>
  );
}


function MemoryPackSnapshotPanel({
  snapshot,
  loading,
  error,
}: {
  snapshot: MemoryPackSnapshotDTO | null;
  loading: boolean;
  error: ShownError | null;
}) {
  const t = useTranslations("admin_turns");
  return (
    <section
      data-testid="admin-replay-snapshot"
      className="legacy-turns-snapshot"
    >
      <h2 className="legacy-turns-snapshot-title">
        {t("snapshot_title")}
      </h2>
      {loading ? (
        <div className="legacy-turns-muted">
          {t("loading")}
        </div>
      ) : error ? (
        <div
          className="approval-card legacy-turns-error"
          data-testid="admin-replay-error"
        >
          <strong>{t("snapshot_error")}</strong> {error.message}
          <ErrorDiagnosticsInline error={error.error} surface="route" />
        </div>
      ) : snapshot === null ? (
        <div
          className="legacy-turns-muted"
          data-testid="admin-replay-empty"
        >
          {t("snapshot_empty")}
        </div>
      ) : (
        <>
          <div className="legacy-turns-kv">
            <div className="legacy-turns-k">turn_id</div>
            <div>{snapshot.turn_id}</div>
            <div className="legacy-turns-k">user_id</div>
            <div>{snapshot.user_id}</div>
            <div className="legacy-turns-k">team_id</div>
            <div>{snapshot.team_id ?? "—"}</div>
            <div className="legacy-turns-k">workspace_id</div>
            <div>{snapshot.workspace_id ?? "—"}</div>
            <div className="legacy-turns-k">pack_version</div>
            <div>{snapshot.pack_version}</div>
            <div className="legacy-turns-k">char_count</div>
            <div>{snapshot.char_count}</div>
            <div className="legacy-turns-k">created_at</div>
            <div>{snapshot.created_at}</div>
            <div className="legacy-turns-k">sections</div>
            <div data-testid="admin-replay-breakdown">
              {Object.entries(snapshot.sections_breakdown)
                .map(([k, v]) => `${k}: ${v}`)
                .join(" · ") || "—"}
            </div>
          </div>
          <details>
            <summary className="legacy-turns-summary">
              {t("snapshot_show_content", { count: snapshot.char_count })}
            </summary>
            <pre
              data-testid="admin-replay-content"
              className="legacy-turns-content"
            >
              {snapshot.content}
            </pre>
          </details>
        </>
      )}
    </section>
  );
}

function badgeFor(event: string): string {
  if (event.startsWith("canvas.")) return "compute";
  if (event.startsWith("pycell.")) return "compute";
  if (event.startsWith("approval.")) return "modeling";
  if (event === "error" || event === "execution_failed") return "causal";
  if (event === "tool_call" || event === "tool_result") return "comparison";
  return "trend";
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n)}…`;
}

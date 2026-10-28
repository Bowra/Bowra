"use client";

// Access requests settings page. Three segments: to review / handled / my requests.
// Approvers see the requester and the item; requesters ("my requests") never see the item
// name or who decided — only the state, the date and the manager's note.

import { useCallback, useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";
import { useLocale, useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { useNarrowViewport } from "@/components/ui/sheet";
import { formatDateYMD } from "@/lib/format";
import { useAppRouter } from "@/lib/use-app-router";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { ApiError } from "@/lib/api-client";
import { grantHasExpired } from "@/components/access/access-request-panel";
import {
  accessRequestErrorCode,
  decideAccessRequest,
  listAccessRequests,
  listMyAccessRequests,
  withdrawAccessRequest,
  type AccessRequestItem,
  type DecideOptions,
  type RequesterRequestView,
} from "@/lib/access-requests-api";

type Scope = "pending" | "history" | "mine";

interface DecisionDraft {
  note: string;
  grant30: boolean;
  stop: boolean;
  more: boolean;
}

const EMPTY_DRAFT: DecisionDraft = { note: "", grant30: false, stop: false, more: false };
const NOTE_MAX = 280;

/** Where the requester opens an item once access is granted. */
export function accessRequestOpenHref(kind: string, resourceId: string): string | null {
  const id = encodeURIComponent(resourceId);
  if (kind === "report") return `/?session=${id}`;
  if (kind === "team_subagent") return `/agents/detail/?id=${id}`;
  if (kind === "team_skill") return `/skills/?skill=${id}`;
  return null;
}

/** Decision failures with their own copy; anything else gets the generic message. */
const DECIDE_ERROR_CODES = new Set([
  "access_request_decision_invalid",
  "access_request_grant_expiry_unsupported",
  "access_request_skill_not_grantable",
  "access_request_requester_inactive",
  "access_request_agent_has_unpublished_changes",
  "access_request_agent_release_changed",
]);

/** The requester-side state, with a time-limited grant that has run out read as expired. */
export function mineDisplayState(view: RequesterRequestView, now: number = Date.now()): RequesterRequestView["state"] {
  return view.state === "approved" && grantHasExpired(view.grant_expires_at, now) ? "expired" : view.state;
}

function kindBadgeKey(kind: string): string {
  if (kind === "team_skill") return "badge_team_skill";
  if (kind === "team_subagent") return "badge_team_subagent";
  return "badge_report";
}

export function AccessRequestsContent() {
  const t = useTranslations("settings.accessRequests");
  const locale = useLocale();
  const router = useAppRouter();
  const narrow = useNarrowViewport(true);
  const meId = useOptionalAuthSession()?.me?.id ?? null;
  const titleRef = useRef<HTMLHeadingElement>(null);
  const sheetReturnRef = useRef<HTMLButtonElement | null>(null);
  const [scope, setScope] = useState<Scope>("pending");
  const [items, setItems] = useState<AccessRequestItem[]>([]);
  const [mine, setMine] = useState<RequesterRequestView[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [drafts, setDrafts] = useState<Record<string, DecisionDraft>>({});
  const [sheetFor, setSheetFor] = useState<AccessRequestItem | null>(null);
  const requestVersionRef = useRef(0);
  const mountedRef = useRef(true);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;

  const statusLabel = useCallback(
    (status: string): string => {
      switch (status) {
        case "approved":
          return t("status_approved");
        case "denied":
          return t("status_denied");
        case "approved_pending":
          return t("status_approved_pending");
        case "withdrawn":
          return t("status_withdrawn");
        case "pending":
          return t("status_pending");
        case "approve_failed":
          return t("status_approve_failed");
        case "stale":
          return t("status_stale");
        default:
          return t("status_expired");
      }
    },
    [t],
  );

  const formatDate = (iso: string | null | undefined) => {
    if (!iso) return "";
    const ms = Date.parse(iso);
    return Number.isFinite(ms) ? new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(ms) : "";
  };

  const load = useCallback(
    async (showLoading = false) => {
      const requestVersion = ++requestVersionRef.current;
      if (showLoading) {
        setLoading(true);
        setItems([]);
        setMine([]);
      }
      try {
        setErr(null);
        if (scope === "mine") {
          const result = await listMyAccessRequests();
          if (requestVersion === requestVersionRef.current) setMine(result.items);
        } else {
          const result = await listAccessRequests(scope);
          if (requestVersion === requestVersionRef.current) setItems(result.items);
        }
      } catch (e) {
        if (requestVersion === requestVersionRef.current) setErr(t("error_load_failed"));
      } finally {
        if (requestVersion === requestVersionRef.current) setLoading(false);
      }
    },
    [scope, t],
  );

  useEffect(() => {
    void load(true);
    const id = window.setInterval(() => void load(), 5000);
    return () => {
      ++requestVersionRef.current;
      window.clearInterval(id);
    };
  }, [load]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      ++requestVersionRef.current;
    };
  }, []);

  const changeScope = (next: Scope) => {
    if (next === scope) return;
    ++requestVersionRef.current;
    setItems([]);
    setMine([]);
    setErr(null);
    setLoading(true);
    setScope(next);
  };

  const draftFor = (id: string) => drafts[id] ?? EMPTY_DRAFT;
  const patchDraft = (id: string, patch: Partial<DecisionDraft>) =>
    setDrafts((current) => ({ ...current, [id]: { ...(current[id] ?? EMPTY_DRAFT), ...patch } }));

  const decideErrorText = (e: unknown): string => {
    const code = accessRequestErrorCode(e);
    if (code && DECIDE_ERROR_CODES.has(code)) return t(`error_${code}`);
    if (e instanceof ApiError && e.status === 404) return t("error_not_found");
    return t("error_decide_failed");
  };

  /** Runs one row action. Busy is cleared before `onSuccess` (e.g. closing the decision sheet)
   *  so focus can return to an enabled control; the list reloads afterwards. */
  const runBusy = async (id: string, work: () => Promise<void>, failureText: (e: unknown) => string, onSuccess?: () => void) => {
    const actionScope = scope;
    setBusy((s) => new Set(s).add(id));
    let ok = false;
    try {
      await work();
      ok = true;
    } catch (e) {
      if (mountedRef.current && scopeRef.current === actionScope) setErr(failureText(e));
    } finally {
      if (mountedRef.current) {
        setBusy((s) => {
          const next = new Set(s);
          next.delete(id);
          return next;
        });
      }
    }
    if (!ok || !mountedRef.current) return;
    if (onSuccess) {
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
      onSuccess();
    }
    if (scopeRef.current === actionScope) await load();
    // The decided row leaves the list; keep keyboard users on the page instead of <body>.
    if (mountedRef.current && document.activeElement === document.body) titleRef.current?.focus();
  };

  const decide = (row: AccessRequestItem, action: "approve" | "deny") =>
    runBusy(
      row.id,
      async () => {
        const draft = draftFor(row.id);
        const options: DecideOptions = { note: draft.note };
        if (action === "approve" && draft.grant30 && row.kind !== "team_subagent") options.grantExpiresInDays = 30;
        if (action === "deny" && draft.stop) options.stopRequests = true;
        await decideAccessRequest(row.id, action, options);
        setDrafts((current) => {
          const next = { ...current };
          delete next[row.id];
          return next;
        });
      },
      decideErrorText,
      () => setSheetFor(null),
    );

  const withdraw = (view: RequesterRequestView) =>
    runBusy(
      view.request_id,
      async () => {
        await withdrawAccessRequest(view.request_id);
      },
      () => t("withdraw_failed"),
    );

  const empty = (text: string) => <div className="legacy-acr-empty acr-empty">{text}</div>;
  const grantsText = (kind: string) => (kind === "report" ? t("grants_read") : t("grants_use"));

  const decisionFields = (row: AccessRequestItem, idPrefix: string) => {
    const draft = draftFor(row.id);
    const disabled = busy.has(row.id);
    return (
      <>
        <input
          id={`${idPrefix}-note`}
          className="acr-x-input"
          value={draft.note}
          maxLength={NOTE_MAX}
          placeholder={t("note_placeholder")}
          aria-label={t("note_placeholder")}
          disabled={disabled}
          onChange={(event) => patchDraft(row.id, { note: event.target.value.slice(0, NOTE_MAX) })}
        />
        {row.kind !== "team_subagent" ? (
          <label className="acr-x-check">
            <input
              type="checkbox"
              checked={draft.grant30}
              disabled={disabled}
              onChange={(event) => patchDraft(row.id, { grant30: event.target.checked })}
            />
            {t("grant_30_days")}
          </label>
        ) : null}
      </>
    );
  };

  const moreOptions = (row: AccessRequestItem, idPrefix: string) => {
    const draft = draftFor(row.id);
    const panelId = `${idPrefix}-more`;
    return {
      toggle: (
        <button
          type="button"
          className="acr-x-more"
          aria-expanded={draft.more}
          aria-controls={panelId}
          onClick={() => patchDraft(row.id, { more: !draft.more })}
        >
          {t("more_options")}
        </button>
      ),
      panel: draft.more ? (
        <div id={panelId} className="acr-x-decide-row">
          <label className="acr-x-check">
            <input
              type="checkbox"
              checked={draft.stop}
              disabled={busy.has(row.id)}
              onChange={(event) => patchDraft(row.id, { stop: event.target.checked })}
            />
            {t("stop_requests")}
          </label>
        </div>
      ) : null,
    };
  };

  const renderApproverRow = (r: AccessRequestItem) => {
    const disabled = busy.has(r.id);
    // Nobody decides their own request; the server refuses it too.
    const canDecide = scope === "pending" && r.requester_id !== meId;
    const more = moreOptions(r, `acr-${r.id}`);
    return (
      <div key={r.id} data-testid={`acr-row-${r.id}`} className="legacy-acr-item acr-item">
        <div className="legacy-acr-head">
          <Badge size="sm">{t(kindBadgeKey(r.kind))}</Badge>
          <span className="legacy-acr-label">{r.label}</span>
          <span className="acr-x-grants">· {grantsText(r.kind)}</span>
          {r.prior_denied > 0 ? (
            <span className="legacy-acr-denied">{t("prior_denied", { count: r.prior_denied })}</span>
          ) : null}
          <span className="legacy-acr-spacer" />
          {scope === "history" ? <span className="legacy-acr-status">{statusLabel(r.status)}</span> : null}
        </div>
        <div className="legacy-acr-meta">
          <strong className="legacy-acr-name">{r.requester_name}</strong>
          {r.requester_email ? (
            <span className="legacy-acr-email">{t("requester_email", { email: r.requester_email })}</span>
          ) : null}
          {t("requested_access")} {formatDateYMD(r.created_at)}
        </div>
        <div className={`legacy-acr-message${r.message ? "" : " is-empty"}`}>{r.message || t("no_reason")}</div>
        {scope === "history" && r.decision_note ? (
          <div className="acr-x-note">{t("decision_note", { note: r.decision_note })}</div>
        ) : null}
        {scope === "history" && r.status === "approved" && r.grant_expires_at ? (
          <div className="acr-x-note">{t("grant_until", { date: formatDate(r.grant_expires_at) })}</div>
        ) : null}
        {canDecide && narrow ? (
          <div className="legacy-acr-actions">
            <Button
              size="sm"
              variant="primary"
              disabled={disabled}
              onClick={(event) => {
                sheetReturnRef.current = event.currentTarget;
                setSheetFor(r);
              }}
            >
              {t("action_review")}
            </Button>
          </div>
        ) : null}
        {canDecide && !narrow ? (
          <div className="acr-x-decide">
            <div className="acr-x-decide-row">{decisionFields(r, `acr-${r.id}`)}</div>
            <div className="acr-x-decide-row">
              {more.toggle}
              <span className="acr-x-spacer" />
              <Button size="sm" variant="secondary" disabled={disabled} onClick={() => void decide(r, "deny")}>
                {t("action_deny")}
              </Button>
              <Button size="sm" variant="primary" disabled={disabled} onClick={() => void decide(r, "approve")}>
                {disabled ? t("action_processing") : t("action_approve")}
              </Button>
            </div>
            {more.panel}
          </div>
        ) : null}
      </div>
    );
  };

  const renderMineRow = (raw: RequesterRequestView) => {
    const v = { ...raw, state: mineDisplayState(raw) };
    const href = v.state === "approved" ? accessRequestOpenHref(v.kind, v.resource_id) : null;
    const tone = v.state === "approved" ? "positive" : v.state === "pending" ? "accent" : "neutral";
    return (
      <div key={v.request_id} data-testid={`acr-mine-${v.request_id}`} className="legacy-acr-item acr-item">
        <div className="acr-x-mine-row">
          <Badge size="sm">{t(kindBadgeKey(v.kind))}</Badge>
          <span>{t("mine_from_link", { date: formatDate(v.created_at) })}</span>
          <span className="acr-x-spacer" />
          <Badge size="sm" tone={tone}>
            {statusLabel(v.state)}
          </Badge>
          {v.state === "pending" ? (
            <Button size="sm" variant="ghost" disabled={busy.has(v.request_id)} onClick={() => void withdraw(v)}>
              {t("mine_withdraw")}
            </Button>
          ) : null}
          {href ? (
            <Button size="sm" variant="ghost" onClick={() => router.push(href)}>
              {t("mine_open")}
            </Button>
          ) : null}
        </div>
        {v.note ? <div className="legacy-acr-message">{v.note}</div> : null}
        {v.state === "denied" && v.retry_at ? (
          <div className="acr-x-note">{t("mine_retry_at", { date: formatDate(v.retry_at) })}</div>
        ) : null}
        {v.state === "approved" && v.grant_expires_at ? (
          <div className="acr-x-note">{t("grant_until", { date: formatDate(v.grant_expires_at) })}</div>
        ) : null}
        {v.state === "expired" ? <div className="acr-x-note">{t("status_mine_expired")}</div> : null}
      </div>
    );
  };

  const tabs: { scope: Scope; label: string }[] = [
    { scope: "pending", label: t("tab_to_review") },
    { scope: "history", label: t("tab_handled") },
    { scope: "mine", label: t("tab_mine") },
  ];
  const emptyText = scope === "pending" ? t("empty_pending") : scope === "history" ? t("empty_history") : t("empty_mine");
  const count = scope === "mine" ? mine.length : items.length;

  return (
    <div className="legacy-acr-page" data-testid="access-requests-page">
      <h1 className="ui-page-title legacy-access-requests-title" ref={titleRef} tabIndex={-1}>
        {t("title")}
      </h1>
      <p className="ui-page-desc legacy-access-requests-desc">{t("subtitle")}</p>

      <div className="acr-x-seg" role="group" aria-label={t("scope_label")}>
        {tabs.map((tab) => (
          <button
            key={tab.scope}
            type="button"
            aria-pressed={scope === tab.scope}
            onClick={() => changeScope(tab.scope)}
            className={`acr-x-seg-btn${scope === tab.scope ? " is-active" : ""}`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {loading ? (
        <LoadingSkeleton rows={3} label={t("loading")} />
      ) : (
        <>
          {err ? <Notice tone="error">{err}</Notice> : null}
          {!err && count === 0 ? empty(emptyText) : null}
          {count > 0 ? (
            <div className="legacy-acr-list acr-list">
              {scope === "mine" ? mine.map(renderMineRow) : items.map(renderApproverRow)}
            </div>
          ) : null}
        </>
      )}

      {sheetFor ? (
        <DecisionSheet
          row={sheetFor}
          busy={busy.has(sheetFor.id)}
          grants={grantsText(sheetFor.kind)}
          fields={decisionFields(sheetFor, `acr-sheet-${sheetFor.id}`)}
          more={moreOptions(sheetFor, `acr-sheet-${sheetFor.id}`)}
          returnFocusRef={sheetReturnRef}
          onApprove={() => void decide(sheetFor, "approve")}
          onDeny={() => void decide(sheetFor, "deny")}
          onClose={() => setSheetFor(null)}
        />
      ) : null}
    </div>
  );
}

function DecisionSheet({
  row,
  busy,
  grants,
  fields,
  more,
  returnFocusRef,
  onApprove,
  onDeny,
  onClose,
}: {
  row: AccessRequestItem;
  busy: boolean;
  grants: string;
  fields: ReactNode;
  more: { toggle: ReactNode; panel: ReactNode };
  returnFocusRef: RefObject<HTMLButtonElement | null>;
  onApprove: () => void;
  onDeny: () => void;
  onClose: () => void;
}) {
  const t = useTranslations("settings.accessRequests");
  const describedBy = useId();
  return (
    <Dialog
      title={t("decide_sheet_title", { name: row.requester_name })}
      onClose={onClose}
      busy={busy}
      presentation="sheet"
      describedBy={describedBy}
      returnFocusRef={returnFocusRef}
    >
      <div className="acr-x-sheet">
        <p id={describedBy} className="acr-x-grants">
          {row.label} · {grants}
        </p>
        {fields}
        {more.toggle}
        {more.panel}
        <div className="acr-x-sheet-actions">
          <Button variant="primary" disabled={busy} onClick={onApprove}>
            {busy ? t("action_processing") : t("action_approve")}
          </Button>
          <Button variant="danger" disabled={busy} onClick={onDeny}>
            {t("action_deny")}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

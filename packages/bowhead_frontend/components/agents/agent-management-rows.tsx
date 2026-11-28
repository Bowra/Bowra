"use client";

/**
 * My Agents rows with FEATURE_AGENT_RELEASE_BAR on (slices 2-5 tech design §5): one primary per row, the
 * rest in "⋯", and a status chip only for exceptions. The parent page keeps every request and dialog.
 */
import type { MouseEvent, ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import type { AgentCatalogItem, AgentManagementView } from "@/lib/agents-api";
import { rowException, type CreatedFilter, type CreatedListQuery, type CreatedSort } from "@/lib/agent-management-list";
import { withAgentReturnView } from "@/lib/agent-navigation";
import { appPath } from "@/lib/base-path";
import { formatDateTime } from "@/lib/locale";
import type { SubagentDraftListItem } from "@/lib/subagent-drafts-api";
import { CapabilityLink } from "@/components/capabilities/capability-link";
import { useListDate } from "@/components/ui/list-time";
import { RowActionsMenu, type RowAction } from "@/components/ui/row-actions-menu";
import { agentArtworkStyle } from "./agent-artwork";
import { agentMark } from "./agent-mark";
import { useAgentListingText } from "./agent-listing-text";
import { LimitedStatus, ReasonLine, ReasonRetry, useAvailabilityReason } from "./availability-reason";

export interface ManagedRowHost {
  view: AgentManagementView;
  /** Another row's request is running: every row action waits. */
  pending: boolean;
  canDirectUse: boolean;
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
  onStartUse: (item: AgentCatalogItem) => void;
  onHire: (item: AgentCatalogItem) => void;
  onSetStatus: (item: AgentCatalogItem, status: "active" | "disabled") => void;
  /** Opens the unhire confirmation; focus returns to `trigger` afterwards. */
  onUnhire: (item: AgentCatalogItem, trigger: HTMLElement) => void;
  onRetry: () => void;
}

function detailPath(item: AgentCatalogItem, view: AgentManagementView): string {
  return withAgentReturnView(`/agents/detail/?id=${encodeURIComponent(item.agent_id)}`, view);
}

/** The row's own "⋯" trigger, where the unhire dialog returns focus. */
function rowMoreButton(agentId: string): HTMLElement {
  const row = Array.from(document.querySelectorAll<HTMLElement>(".am-management-row[data-agent-id]"))
    .find((element) => element.dataset.agentId === agentId);
  return row?.querySelector<HTMLElement>(".ui-row-more") ?? document.body;
}

function Identity({ item, host }: { item: AgentCatalogItem; host: ManagedRowHost }) {
  const { displayName, summary } = useAgentListingText();
  const name = displayName(item);
  const body = <>
    <span className="am-management-mark" style={agentArtworkStyle(`${item.agent_id}:${name}`)} aria-hidden="true">{agentMark(name)}</span>
    <div><h2>{name}</h2><p>{summary(item)}</p></div>
  </>;
  if (!item.actions.view_detail) return <div className="am-management-identity">{body}</div>;
  const path = detailPath(item, host.view);
  return <a className="am-management-identity" href={appPath(path)} onClick={(event) => host.onNavigate(event, path)}>{body}</a>;
}

/** Message suffix for an exception: the viewer's own archived Agent says 你把它归档了, not 所有者把它归档了. */
function exceptionCopy(item: AgentCatalogItem, exception: NonNullable<ReturnType<typeof rowException>>) {
  return exception === "archived_by_owner" && item.relationship === "owned" ? "archived_by_you" : exception;
}

function ExceptionChip({ item, view }: { item: AgentCatalogItem; view: "created" | "hired" | "shared" }) {
  const t = useTranslations("agent_list_v2");
  const exception = rowException(item, view);
  if (!exception) return null;
  return <span data-status={`v2-${exception}`}>{t(`chip_${exceptionCopy(item, exception)}`)}</span>;
}

/** Who provides a row that is not the viewer's own: kept for hired and shared rows. */
function SourceLine({ item }: { item: AgentCatalogItem }) {
  const t = useTranslations("agents_marketplace");
  const tList = useTranslations("agent_list_v2");
  if (item.relationship === "owned") return <span data-status="v2-owner">{tList("chip_owner")}</span>;
  if (item.source.kind === "system") return <small>{t("source_system")}</small>;
  return item.source.owner_label ? <small>{t("by_creator", { name: item.source.owner_label })}</small> : null;
}

function ListingBlocked({ item }: { item: AgentCatalogItem }) {
  const t = useTranslations("agents_marketplace");
  return item.listing_safety?.effective_status === "blocked" ? <span data-status="listing-blocked">{t("listing_safety_blocked")}</span> : null;
}

/** One row of 我创建的, 我聘用的 (runnable) or 分享给我的. */
export function ManagedAgentRow({ item, host }: { item: AgentCatalogItem; host: ManagedRowHost }) {
  const t = useTranslations("agents_marketplace");
  const tList = useTranslations("agent_list_v2");
  const listDate = useListDate();
  const { displayName } = useAgentListingText();
  const name = displayName(item);
  const view = host.view === "created" || host.view === "hired" ? host.view : "shared";
  const canStart = host.canDirectUse && item.availability.runnable && item.assignment.status === "active";
  const startUse: RowAction = { id: "start_use", label: t("start_use"), disabled: host.pending, onSelect: () => host.onStartUse(item) };
  const statusActions: RowAction[] = [
    ...(item.actions.disable ? [{ id: "pause", label: t("pause"), disabled: host.pending, onSelect: () => host.onSetStatus(item, "disabled") }] : []),
    ...(item.actions.enable ? [{ id: "enable", label: t("enable"), disabled: host.pending, onSelect: () => host.onSetStatus(item, "active") }] : []),
  ];
  const unhire: RowAction[] = item.actions.unhire ? [{
    id: "unhire", label: t("unhire"), danger: true, disabled: host.pending,
    onSelect: () => host.onUnhire(item, rowMoreButton(item.agent_id)),
  }] : [];
  const hireAction: RowAction[] = item.actions.hire ? [{ id: "hire", label: t("hire"), disabled: host.pending, onSelect: () => host.onHire(item) }] : [];

  let primary: ReactNode = null;
  let more: RowAction[] = [];
  if (view === "created") {
    primary = item.actions.manage && item.manager_url
      ? <CapabilityLink href={withAgentReturnView(item.manager_url, host.view)} aria-label={tList("manage_named", { name })}>{tList("manage")}</CapabilityLink>
      : null;
    more = [...(canStart ? [startUse] : []), ...hireAction, ...statusActions, ...unhire];
  } else if (view === "hired") {
    primary = canStart
      ? <button type="button" disabled={host.pending} aria-label={tList("start_use_named", { name })} onClick={() => host.onStartUse(item)}>{t("start_use")}</button>
      : null;
    more = [...statusActions, ...unhire];
  } else {
    primary = item.actions.hire
      ? <button type="button" disabled={host.pending} aria-label={tList("hire_named", { name })} onClick={() => host.onHire(item)}>{t("hire")}</button>
      : canStart
        ? <button type="button" disabled={host.pending} aria-label={tList("start_use_named", { name })} onClick={() => host.onStartUse(item)}>{t("start_use")}</button>
        : null;
    more = [...(item.actions.hire && canStart ? [startUse] : []), ...statusActions, ...unhire];
  }
  const updated = view === "created" ? listDate(item.updated_label?.at)?.label : null;
  return (
    <article className="am-management-row" data-agent-id={item.agent_id} data-layout="v2">
      <Identity item={item} host={host} />
      <div className="am-management-status">
        <ListingBlocked item={item} />
        <ExceptionChip item={item} view={view} />
        {view !== "created" ? <SourceLine item={item} /> : null}
        <LimitedStatus item={item} enabled={view === "hired"} />
        {updated ? <small>{tList("updated", { date: updated })}</small> : null}
      </div>
      <div className="am-management-actions">
        {view === "hired" ? <ReasonRetry item={item} disabled={host.pending} onRetry={host.onRetry} /> : null}
        {view === "shared" && item.actions.view_detail ? (
          <a href={appPath(detailPath(item, host.view))} aria-label={tList("view_detail_named", { name })}
            onClick={(event) => host.onNavigate(event, detailPath(item, host.view))}>{tList("view_detail")}</a>
        ) : null}
        {primary}
        <RowActionsMenu label={t("more_actions_for", { name })} pending={host.pending} actions={more} />
      </div>
    </article>
  );
}

/** A hired row that cannot run: its one reason and its one action, in the folded 现在用不了 group. */
export function UnavailableHiredRow({ item, host }: { item: AgentCatalogItem; host: ManagedRowHost }) {
  const t = useTranslations("agents_marketplace");
  const tList = useTranslations("agent_list_v2");
  const { displayName } = useAgentListingText();
  const reason = useAvailabilityReason(item);
  const name = displayName(item);
  const exception = rowException(item, "hired");
  const retry = reason?.action === "retry";
  // The viewer's own Agent that cannot run is fixed on its page (the reason says so): 管理 leads there.
  const manage = item.relationship === "owned" && item.actions.manage && item.manager_url ? item.manager_url : null;
  const action = item.actions.enable ? "enable" : retry ? "retry" : manage ? "manage" : item.actions.unhire ? "unhire" : null;
  return (
    <article className="am-management-row" data-agent-id={item.agent_id} data-layout="v2" data-group="unavailable">
      <Identity item={item} host={host} />
      <div className="am-management-status">
        {exception === "disabled" || exception === "archived_by_owner"
          ? <p className="am-management-reason" data-reason={exception}><span>{tList(`reason_${exceptionCopy(item, exception)}`)}</span></p>
          : reason ? <ReasonLine view={reason} className="am-management-reason" /> : <small>{t("assignment_active_unavailable")}</small>}
      </div>
      <div className="am-management-actions">
        {action === "enable" ? <button type="button" disabled={host.pending} aria-label={tList("reenable_named", { name })} onClick={() => host.onSetStatus(item, "active")}>{tList("reenable")}</button> : null}
        {action === "retry" ? <ReasonRetry item={item} disabled={host.pending} onRetry={host.onRetry} /> : null}
        {action === "manage" && manage ? <CapabilityLink href={withAgentReturnView(manage, host.view)} aria-label={tList("manage_named", { name })}>{tList("manage")}</CapabilityLink> : null}
        {action === "unhire" ? <button type="button" disabled={host.pending} aria-label={tList("unhire_named", { name })} onClick={(event) => host.onUnhire(item, event.currentTarget)}>{tList("unhire")}</button> : null}
        {action !== "unhire" && item.actions.unhire ? (
          <RowActionsMenu label={t("more_actions_for", { name })} pending={host.pending}
            actions={[{ id: "unhire", label: t("unhire"), danger: true, disabled: host.pending, onSelect: () => host.onUnhire(item, rowMoreButton(item.agent_id)) }]} />
        ) : null}
      </div>
    </article>
  );
}

/** 继续创建: each draft says when it expires and offers a quiet 删除…. */
export function CreatedDraftRows({ drafts, busyDraftId, onNavigate, onDelete }: {
  drafts: readonly SubagentDraftListItem[];
  busyDraftId: string | null;
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
  onDelete: (draft: SubagentDraftListItem, trigger: HTMLElement) => void;
}) {
  const t = useTranslations("agents_marketplace");
  const tList = useTranslations("agent_list_v2");
  const locale = useLocale();
  const listDate = useListDate();
  return (
    <section className="am-list-v2-group" aria-labelledby="am-list-v2-drafts">
      <h2 id="am-list-v2-drafts">{tList("drafts_heading")} <span>· {drafts.length}</span></h2>
      <ul className="am-list-v2-drafts">
        {drafts.map((draft) => {
          const path = `/?agent_draft=${encodeURIComponent(draft.draft_id)}`;
          const title = draft.title || t("drafts_untitled");
          const expires = formatDateTime(draft.expires_at, locale, { dateStyle: "medium" });
          return <li key={draft.draft_id}>
            <span className="am-list-v2-draft-mark" aria-hidden="true">{agentMark(title)}</span>
            <div>
              <strong>{title}</strong>
              <small>
                {t(draft.source_mode === "manual" ? "drafts_source_manual" : "drafts_source_ai")}
                {" · "}{t(`drafts_status_${draft.draft_status}` as never)}
                {" · "}{tList("draft_saved", { date: listDate(draft.updated_at)?.label ?? "—" })}
                {expires ? <>{" · "}{tList("draft_expires", { date: expires })}</> : null}
              </small>
            </div>
            <button type="button" className="am-list-v2-quiet" disabled={busyDraftId !== null}
              aria-label={tList("draft_delete_named", { name: title })} onClick={(event) => onDelete(draft, event.currentTarget)}>
              {busyDraftId === draft.draft_id ? tList("draft_deleting") : tList("draft_delete")}
            </button>
            <a href={appPath(path)} aria-label={tList("draft_continue_named", { name: title })} onClick={(event) => onNavigate(event, path)}>{tList("draft_continue")}</a>
          </li>;
        })}
      </ul>
    </section>
  );
}

/** Search, filter and sort for 我创建的 (client side over the loaded rows; decision SD2). */
export function CreatedListControls({ value, onChange }: { value: CreatedListQuery; onChange: (next: CreatedListQuery) => void }) {
  const tList = useTranslations("agent_list_v2");
  return (
    <div className="am-list-v2-controls">
      <label className="am-list-v2-search">
        <span className="sr-only">{tList("search_label")}</span>
        <input type="search" name="agent-list-search" autoComplete="off" value={value.query}
          placeholder={tList("search_placeholder")} onChange={(event) => onChange({ ...value, query: event.target.value })} />
      </label>
      <label>
        <span>{tList("filter_label")}</span>
        <select name="agent-list-filter" value={value.filter} onChange={(event) => onChange({ ...value, filter: event.target.value as CreatedFilter })}>
          <option value="all">{tList("filter_all")}</option>
          <option value="disabled">{tList("filter_disabled")}</option>
          <option value="unavailable">{tList("filter_unavailable")}</option>
        </select>
      </label>
      <label>
        <span>{tList("sort_label")}</span>
        <select name="agent-list-sort" value={value.sort} onChange={(event) => onChange({ ...value, sort: event.target.value as CreatedSort })}>
          <option value="updated">{tList("sort_updated")}</option>
          <option value="name">{tList("sort_name")}</option>
        </select>
      </label>
    </div>
  );
}

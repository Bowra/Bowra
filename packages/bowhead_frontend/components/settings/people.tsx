"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { useEditionMemberDetails } from "@/edition/member-details";
import { MembersTable } from "@/components/admin/members/members-table";
import { Tab, TabList, Tabs } from "@/components/ui/tabs";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { PromoteModal } from "@/components/admin/members/promote-modal";
import { TransferOwnerDialog } from "@/components/admin/members/transfer-owner-dialog";
import { Button } from "@/components/ui/button";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { ActiveFilterChips } from "@/components/ui/active-filter-chips";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { ConfirmDialog, useConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { Folder, Plus, RefreshCw, Shield, Trash, Upload, Users, X } from "@/components/icons";
import { fetchMe } from "@/lib/auth-api";
import { getAuthCapabilities, type AuthCapabilities } from "@/lib/invite-api";
import {
  AdminMembersError,
  type AdminMemberRow,
  type AdminMembersListResp,
  type TeamRole,
  type TeamInviteRow,
  type InitialWorkspaceGrant,
  type WorkspaceGrantRow,
  deactivateTeamMember,
  createLocalMember,
  createCredentialRecovery,
  fetchMemberWorkspaceGrants,
  fetchAdminMembers,
  fetchTeamInvitesPage,
  inviteTeamMember,
  resendTeamInvite,
  revokeCredentialRecovery,
  revokeMemberWorkspaceGrant,
  revokeTeamInvite,
  upsertMemberWorkspaceGrant,
} from "@/lib/admin-members-api";
import {
  fetchTeamSpaces,
  type TeamSpaceRow,
} from "@/lib/admin-spaces-api";
import { appPath } from "@/lib/base-path";
import { copyTextWithFallback } from "@/lib/clipboard";
import { withoutNextRouterHistoryMarkers } from "@/lib/url-session-nav";

const MEMBER_EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/;
const DEFAULT_PAGE_SIZE = 25;
const MOBILE_PAGE_SIZE = 10;
const PAGE_SIZE_OPTIONS = [10, 25, 50] as const;
const MAX_BATCH_INVITES = 25;

function parseBatchEmails(raw: string) {
  const entries = raw.split(/[\s,;]+/).map((value) => value.replace(/^"|"$/g, "").trim().toLowerCase())
    .filter((value) => value && value !== "email");
  const unique = [...new Set(entries)];
  return {
    emails: unique,
    duplicateCount: entries.length - unique.length,
    invalid: unique.filter((email) => !MEMBER_EMAIL_RE.test(email)),
  };
}

export function SettingsPeople() {
  const t = useTranslations("settings.people");
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  const [data, setData] = useState<AdminMembersListResp | null>(null);
  const [invites, setInvites] = useState<TeamInviteRow[]>([]);
  const [invitesTotal, setInvitesTotal] = useState(0);
  const [invitesError, setInvitesError] = useState(false);
  const [invitesLoading, setInvitesLoading] = useState(true);
  const [currentUserId, setCurrentUserId] = useState("");
  const [identityError, setIdentityError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const [memberSearch, setMemberSearch] = useState("");
  const [debouncedMemberSearch, setDebouncedMemberSearch] = useState("");
  const [memberPage, setMemberPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [memberRole, setMemberRole] = useState<TeamRole | "">("");
  const [inviteSearch, setInviteSearch] = useState("");
  const [debouncedInviteSearch, setDebouncedInviteSearch] = useState("");
  const [invitePage, setInvitePage] = useState(1);
  const [queryReady, setQueryReady] = useState(false);
  const [selectedMemberIds, setSelectedMemberIds] = useState<string[]>([]);
  const [bulkRemovalOpen, setBulkRemovalOpen] = useState(false);
  const [bulkRemovalConfirm, setBulkRemovalConfirm] = useState("");
  const [bulkRemovalBusy, setBulkRemovalBusy] = useState(false);
  const [bulkRemovalResult, setBulkRemovalResult] = useState<{ removed: number; failed: string[] } | null>(null);
  const [canViewCredits, setCanViewCredits] = useState(false);
  const [activeView, setActiveView] = useState<"members" | "invites">("members");
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [onboardingDiscardOpen, setOnboardingDiscardOpen] = useState(false);
  const [inviteSpaceSearch, setInviteSpaceSearch] = useState("");
  const inviteSpaceSearchRef = useRef<HTMLInputElement>(null);
  const [target, setTarget] = useState<AdminMemberRow | null>(null);
  const [ownershipTarget, setOwnershipTarget] = useState<AdminMemberRow | null>(null);
  const [workspaceTarget, setWorkspaceTarget] = useState<Pick<AdminMemberRow, "user_id" | "display_name" | "email"> | null>(null);
  const [deactivateTarget, setDeactivateTarget] = useState<AdminMemberRow | null>(null);
  const [recoveryTarget, setRecoveryTarget] = useState<AdminMemberRow | null>(null);
  const memberDetails = useEditionMemberDetails();
  const [inviteEmail, setInviteEmail] = useState("");
  const [batchInviteMode, setBatchInviteMode] = useState(false);
  const [batchInviteText, setBatchInviteText] = useState("");
  const [batchInviteProgress, setBatchInviteProgress] = useState(0);
  const [batchInviteResults, setBatchInviteResults] = useState<{
    email: string; result: string; url?: string; requiresCopy: boolean;
  }[]>([]);
  const [copiedBatchInviteEmails, setCopiedBatchInviteEmails] = useState<string[]>([]);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [inviteLink, setInviteLink] = useState<{ url: string; emailSent: boolean } | null>(null);
  const [inviteMessage, setInviteMessage] = useState<string | null>(null);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [inviteEmailError, setInviteEmailError] = useState<string | null>(null);
  const [spaceAdminV2, setSpaceAdminV2] = useState(false);
  const [inviteSpaces, setInviteSpaces] = useState<TeamSpaceRow[]>([]);
  const [inviteSpacesLoading, setInviteSpacesLoading] = useState(false);
  const [inviteSpacesError, setInviteSpacesError] = useState(false);
  const [inviteAssignmentMode, setInviteAssignmentMode] = useState<"spaces" | "team_only">("spaces");
  const [inviteWorkspaceIds, setInviteWorkspaceIds] = useState<string[]>([]);
  const [inviteWorkspaceRole, setInviteWorkspaceRole] = useState<InitialWorkspaceGrant["role"]>("member");
  const [createEmail, setCreateEmail] = useState("");
  const [createName, setCreateName] = useState("");
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createMessage, setCreateMessage] = useState<string | null>(null);
  const [createdCredentials, setCreatedCredentials] = useState<{
    memberId: string;
    username: string;
    recoveryUrl: string;
    expiresAt: string;
  } | null>(null);
  const [authCapabilities, setAuthCapabilities] = useState<AuthCapabilities | null>(null);

  const reload = useCallback(() => {
    setSelectedMemberIds([]);
    setRefreshTick((value) => value + 1);
  }, []);

  function changePageSize(size: number) {
    setSelectedMemberIds([]);
    setMemberPage(1);
    setInvitePage(1);
    setPageSize(size);
  }

  useEffect(() => {
    const params = new URL(window.location.href).searchParams;
    setActiveView(params.get("view") === "invites" ? "invites" : "members");
    setMemberSearch((params.get("q") ?? "").slice(0, 120));
    setDebouncedMemberSearch((params.get("q") ?? "").slice(0, 120));
    setInviteSearch((params.get("invite_q") ?? "").slice(0, 120));
    setDebouncedInviteSearch((params.get("invite_q") ?? "").slice(0, 120));
    const role = params.get("role");
    if (role === "owner" || role === "team_admin" || role === "audit_admin" || role === "member") setMemberRole(role);
    const page = Number(params.get("page"));
    const pendingPage = Number(params.get("invite_page"));
    if (Number.isSafeInteger(page) && page > 1) setMemberPage(page);
    if (Number.isSafeInteger(pendingPage) && pendingPage > 1) setInvitePage(pendingPage);
    const requestedSize = Number(params.get("size"));
    if (PAGE_SIZE_OPTIONS.some((size) => size === requestedSize)) setPageSize(requestedSize);
    else if (window.matchMedia?.("(max-width: 720px)").matches) setPageSize(MOBILE_PAGE_SIZE);
    setQueryReady(true);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedMemberSearch(memberSearch.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [memberSearch]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedInviteSearch(inviteSearch.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [inviteSearch]);

  useEffect(() => {
    if (!queryReady) return;
    const url = new URL(window.location.href);
    for (const key of ["q", "role", "page", "size", "invite_q", "invite_page"]) url.searchParams.delete(key);
    if (debouncedMemberSearch) url.searchParams.set("q", debouncedMemberSearch);
    if (memberRole) url.searchParams.set("role", memberRole);
    if (memberPage > 1) url.searchParams.set("page", String(memberPage));
    url.searchParams.set("size", String(pageSize));
    if (debouncedInviteSearch) url.searchParams.set("invite_q", debouncedInviteSearch);
    if (invitePage > 1) url.searchParams.set("invite_page", String(invitePage));
    window.history.replaceState(withoutNextRouterHistoryMarkers(window.history.state), "", url);
  }, [debouncedInviteSearch, debouncedMemberSearch, invitePage, memberPage, memberRole, pageSize, queryReady]);

  useEffect(() => { setSelectedMemberIds([]); }, [debouncedMemberSearch, memberPage, memberRole]);

  function selectView(view: "members" | "invites") {
    setActiveView(view);
    const url = new URL(window.location.href);
    if (view === "invites") url.searchParams.set("view", "invites");
    else url.searchParams.delete("view");
    window.history.replaceState(withoutNextRouterHistoryMarkers(window.history.state), "", url);
  }

  async function closeOnboarding() {
    if (inviteBusy || createBusy) return;
    if (batchInviteResults.some((item) => item.requiresCopy && !copiedBatchInviteEmails.includes(item.email))) {
      const ok = await confirm({
        title: t("batch_invite_uncopied_title"),
        description: t("batch_invite_uncopied_warning"),
        confirmLabel: t("close_anyway"),
        cancelLabel: t("back_to_copy"),
      });
      if (!ok) return;
    }
    const hasDraft = inviteEmail.trim() || batchInviteText.trim() || createEmail.trim() || createName.trim() || inviteWorkspaceIds.length;
    if (hasDraft && !inviteMessage && !batchInviteResults.length && !createMessage) {
      setOnboardingDiscardOpen(true);
      return;
    }
    discardOnboarding();
  }

  function discardOnboarding() {
    setOnboardingDiscardOpen(false);
    setOnboardingOpen(false);
    setInviteEmail("");
    setBatchInviteText("");
    setBatchInviteResults([]);
    setCopiedBatchInviteEmails([]);
    setBatchInviteMode(false);
    setInviteWorkspaceIds([]);
    setInviteSpaceSearch("");
    setCreateEmail("");
    setCreateName("");
    setInviteError(null);
    setInviteEmailError(null);
    setCreateError(null);
  }

  useEffect(() => {
    if (!queryReady) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    void fetchAdminMembers({ page: memberPage, size: pageSize, q: debouncedMemberSearch, role: memberRole })
      .then((result) => {
        if (cancelled) return;
        setData(result);
        const lastPage = Math.max(1, Math.ceil((result.total ?? 0) / pageSize));
        if (memberPage > lastPage) {
          setMemberPage(lastPage);
        }
      })
      .catch((reason) => {
        if (!cancelled) setError(t("err_generic"));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [debouncedMemberSearch, memberPage, memberRole, pageSize, queryReady, refreshTick]);

  useEffect(() => {
    let cancelled = false;
    setIdentityError(false);
    setCurrentUserId("");
    void Promise.allSettled([fetchMe(), getAuthCapabilities()]).then(([meResult, capabilitiesResult]) => {
      if (cancelled) return;
      if (meResult.status === "fulfilled") {
        setCurrentUserId(meResult.value.id);
        setSpaceAdminV2(meResult.value.capabilities?.space_admin_v2 === true);
        setCanViewCredits(meResult.value.credit_capabilities?.credits_read === true);
      } else {
        setSpaceAdminV2(false);
        setCanViewCredits(false);
        setIdentityError(true);
      }
      setAuthCapabilities(capabilitiesResult.status === "fulfilled" ? capabilitiesResult.value : null);
    });
    return () => { cancelled = true; };
  }, [refreshTick]);

  useEffect(() => {
    if (!queryReady) return;
    let cancelled = false;
    setInvitesLoading(true);
    void fetchTeamInvitesPage({ page: invitePage, size: pageSize, q: debouncedInviteSearch })
      .then((result) => {
        if (cancelled) return;
        setInvites(result.invites);
        setInvitesTotal(result.total);
        const lastPage = Math.max(1, Math.ceil(result.total / pageSize));
        if (invitePage > lastPage) {
          setInvitePage(lastPage);
        }
        setInvitesError(false);
      })
      .catch(() => {
        if (cancelled) return;
        setInvites([]);
        setInvitesTotal(0);
        setInvitesError(true);
      })
      .finally(() => { if (!cancelled) setInvitesLoading(false); });
    return () => { cancelled = true; };
  }, [debouncedInviteSearch, invitePage, pageSize, queryReady, refreshTick]);

  const identityVerified = Boolean(currentUserId) && !identityError;
  const userRoleCanManageMembers = data?.your_role === "owner" || data?.your_role === "team_admin";
  const userRoleCanEdit = data?.your_role === "owner";
  const userRoleCanDeactivate = userRoleCanManageMembers;
  const userRoleCanViewWorkspaceGrants = data?.your_role === "owner"
    || data?.your_role === "team_admin"

    || data?.your_role === "audit_admin";
  const canEditRoles =
    Boolean(data) && !error && identityVerified && userRoleCanEdit;
  const canTransferOwnership = canEditRoles;
  const canManageMembers =
    Boolean(data) && !error && identityVerified && userRoleCanManageMembers;
  const canDeactivateMembers =
    Boolean(data) && Boolean(authCapabilities) && !error && identityVerified && userRoleCanDeactivate;
  const canViewWorkspaceGrants =
    Boolean(data) && !error && identityVerified && userRoleCanViewWorkspaceGrants;
  const canManageWorkspaceGrants = canManageMembers;
  const directCreateAvailable = authCapabilities?.deployment_mode === "local";
  const invitationCreationAvailable = authCapabilities?.deployment_mode === "saas";
  const showInvites = invitationCreationAvailable || invites.length > 0 || invitesError
    || activeView === "invites" || Boolean(inviteSearch || debouncedInviteSearch);

  useEffect(() => {
    let cancelled = false;
    if (!spaceAdminV2 || !canManageMembers) {
      setInviteSpaces([]);
      setInviteWorkspaceIds([]);
      return () => {
        cancelled = true;
      };
    }
    setInviteSpacesLoading(true);
    setInviteSpacesError(false);
    void fetchTeamSpaces({ status: "active" })
      .then((spaces) => {
        if (cancelled) return;
        setInviteSpaces(spaces);
        if (spaces.length === 0) setInviteAssignmentMode("team_only");
      })
      .catch(() => {
        if (!cancelled) {
          setInviteSpaces([]);
          setInviteSpacesError(true);
        }
      })
      .finally(() => {
        if (!cancelled) setInviteSpacesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [canManageMembers, refreshTick, spaceAdminV2]);

  const nonOwnerTeamAdminCount = data?.non_owner_team_admin_count ?? data?.items.filter(
    (member) => member.team_role === "team_admin" && !member.is_owner,
  ).length ?? 0;
  const memberTotal = data?.total ?? data?.items.length ?? 0;
  const memberSearchPending = memberSearch.trim() !== debouncedMemberSearch;
  const inviteSearchPending = inviteSearch.trim() !== debouncedInviteSearch;
  const memberResultsPending = loading || memberSearchPending;
  const inviteResultsPending = invitesLoading || inviteSearchPending;
  const memberFilterActive = Boolean(memberSearch.trim() || debouncedMemberSearch || memberRole);
  const inviteFilterActive = Boolean(inviteSearch.trim() || debouncedInviteSearch);
  const selectedMembers = data?.items.filter((member) => selectedMemberIds.includes(member.user_id)) ?? [];
  const batchEmails = parseBatchEmails(batchInviteText);
  const filteredInviteSpaces = inviteSpaces.filter((space) => space.name.toLowerCase()
    .includes(inviteSpaceSearch.trim().toLowerCase()));
  const hasStaleData = Boolean(error && data);
  const isReadOnlyByRole = Boolean(data) && identityVerified && !userRoleCanManageMembers;
  const roleLabel = data ? t(`role_${data.your_role}`) : "";
  const accessModeLabel = canEditRoles
    ? t("editable")
    : canDeactivateMembers
      ? t("access_limited")
      : t("read_only");
  const displayView = showInvites ? activeView : "members";

  function formatActionError(e: unknown): string {
    if (e instanceof AdminMembersError) {
      if (e.code === "resend_cooldown_1min") return t("err_resend_cooldown");
      if (e.code === "last_workspace_admin") return t("err_last_workspace_admin");
      if (e.code === "workspace_not_found") return t("err_workspace_not_found");
      if (e.code === "workspace_team_mismatch") return t("err_workspace_team_mismatch");
      if (e.code === "grant_not_found") return t("err_grant_not_found");
      if (e.code === "cannot_self_deactivate") return t("err_cannot_self_deactivate");
      if (e.code === "owner_immutable") return t("err_owner_immutable");
      if (e.code === "last_team_admin") return t("err_last_team_admin");
      if (e.code === "already_inactive") return t("err_already_inactive");
      if (e.code === "deactivate_failed") return t("err_deactivate_failed");
      if (e.code === "invite_not_found") return t("err_invite_not_found");
      if (e.code === "invite_status_revoked") return t("err_invite_revoked");
      if (e.code === "invite_status_consumed") return t("err_invite_consumed");
      if (e.code === "invite_status_satisfied") return t("err_invite_satisfied");
      if (e.code === "invite_status_expired") return t("err_invite_expired");
      if (e.code === "invite_not_reissuable") return t("err_invite_not_reissuable");
      if (e.code === "target_user_not_found") return t("err_target_not_found");
      if (e.code === "member_not_found") return t("err_member_not_found");
      if (e.code === "cross_team_target_not_allowed") return t("err_cross_team");
      if (e.code === "readonly_team" || e.code === "team_readonly") return t("err_readonly");
      if (e.code === "email_already_registered") return t("err_email_already_registered");
      if (e.code === "admin_direct_password_disabled") return t("err_direct_create_disabled");
      if (e.code === "local_only") return t("err_local_only");
      if (e.code === "team_not_found") return t("err_team_not_found");
      if (e.code === "member_create_conflict") return t("err_member_create_conflict");
      if (e.code === "seat_exceeded") return t("err_seat_exceeded");
      if (e.code === "seat_full") return t("err_seat_full");
      if (e.code === "over_grace") return t("err_over_grace");
      if (e.code === "suspended") return t("err_suspended");
      if (e.code === "needs_license") return t("err_needs_license");
      if (e.code === "tamper") return t("err_license_invalid");
      if (e.code === "clock_tamper") return t("err_clock_invalid");
      if (e.code === "feature_disabled_use_direct_create") {
        return t("err_invite_activation_unavailable");
      }
      if (e.code === "user_not_found") return t("err_invite_recipient_unavailable");
      if (e.code === "self_recovery_not_allowed") return t("err_self_recovery_not_allowed");
      if (e.code === "shared_identity_recovery_requires_self_service") {
        return t("err_shared_identity_recovery");
      }
      if (e.code === "owner_recovery_requires_owner") {
        return t("err_owner_recovery_requires_owner");
      }
      if (e.code === "credential_recovery_not_allowed") {
        return t("err_credential_recovery_not_allowed");
      }
      if (e.code === "rate_limited" || e.code === "rate_limit") {
        return t("err_rate_limited");
      }
      if (e.code === "feature_disabled") return t("err_feature_disabled");
      return t("err_generic");
    }
    return t("err_generic");
  }

  async function sendInvite() {
    const email = inviteEmail.trim().toLowerCase();
    if (!email || !canManageMembers) return;
    setInviteError(null);
    setInviteEmailError(null);
    setInviteMessage(null);
    setInviteLink(null);
    if (!MEMBER_EMAIL_RE.test(email)) {
      setInviteEmailError(t("err_invite_invalid_email"));
      return;
    }
    if (spaceAdminV2 && inviteAssignmentMode === "spaces" && inviteWorkspaceIds.length === 0) {
      setInviteError(t("err_invite_space_required"));
      return;
    }
    setInviteBusy(true);
    try {
      const assignment = spaceAdminV2
        ? {
            assignment_mode: inviteAssignmentMode,
            initial_workspace_grants:
              inviteAssignmentMode === "spaces"
                ? inviteWorkspaceIds.map((workspaceId) => ({
                    workspace_id: workspaceId,
                    role: inviteWorkspaceRole,
                  }))
                : [],
          }
        : undefined;
      const resp = assignment
        ? await inviteTeamMember(email, assignment)
        : await inviteTeamMember(email);
      if (resp.method === "already_member") {
        setInviteMessage(t("invite_already_member"));
      } else if (resp.method === "already_invited") {
        setInviteMessage(t("invite_already_invited"));
      } else if (resp.method === "grant_merged" || resp.method === "already_present") {
        setInviteMessage(t("invite_assignment_updated"));
      } else {
        setInviteMessage(t(resp.email_sent ? "invite_sent_email" : "invite_sent_url"));
        if (resp.invite_url) {
          setInviteLink({ url: resp.invite_url, emailSent: Boolean(resp.email_sent) });
        }
      }
      setInviteEmail("");
      setInviteWorkspaceIds([]);
      setInviteSpaceSearch("");
      reload();
    } catch (e) {
      if (e instanceof AdminMembersError && e.status === 422 && e.code === "error") {
        setInviteEmailError(t("err_invite_invalid_email"));
      } else {
        setInviteError(formatActionError(e));
      }
    } finally {
      setInviteBusy(false);
    }
  }

  async function sendBatchInvites() {
    if (!canManageMembers || batchEmails.emails.length === 0 || batchEmails.emails.length > MAX_BATCH_INVITES
      || batchEmails.invalid.length > 0 || inviteBusy) return;
    if (spaceAdminV2 && inviteAssignmentMode === "spaces" && inviteWorkspaceIds.length === 0) {
      setInviteError(t("err_invite_space_required"));
      return;
    }
    const assignment = spaceAdminV2 ? {
      assignment_mode: inviteAssignmentMode,
      initial_workspace_grants: inviteAssignmentMode === "spaces"
        ? inviteWorkspaceIds.map((workspaceId) => ({ workspace_id: workspaceId, role: inviteWorkspaceRole }))
        : [],
    } : undefined;
    setInviteBusy(true);
    setInviteError(null);
    setInviteMessage(null);
    setBatchInviteResults([]);
    setCopiedBatchInviteEmails([]);
    setBatchInviteProgress(0);
    for (const email of batchEmails.emails) {
      try {
        const response = await inviteTeamMember(email, assignment);
        const result = response.method === "already_member" ? t("invite_already_member")
          : response.method === "already_invited" ? t("invite_already_invited")
            : response.method === "grant_merged" || response.method === "already_present"
              ? t("invite_assignment_updated")
              : t(response.email_sent ? "invite_sent_email" : "invite_sent_url");
        setBatchInviteResults((current) => [...current, {
          email, result, url: response.invite_url,
          requiresCopy: Boolean(response.invite_url && !response.email_sent),
        }]);
      } catch (error) {
        setBatchInviteResults((current) => [...current, { email, result: formatActionError(error), requiresCopy: false }]);
      }
      setBatchInviteProgress((current) => current + 1);
    }
    setInviteBusy(false);
    reload();
  }

  async function removeSelectedMembers() {
    if (!canDeactivateMembers || bulkRemovalConfirm !== String(selectedMembers.length)
      || selectedMembers.length === 0 || bulkRemovalBusy) return;
    setBulkRemovalBusy(true);
    const failed: string[] = [];
    let removed = 0;
    for (const member of selectedMembers) {
      try {
        await deactivateTeamMember(member.user_id);
        removed += 1;
      } catch (error) {
        failed.push(`${member.display_name || member.email}: ${formatActionError(error)}`);
      }
    }
    setBulkRemovalResult({ removed, failed });
    setBulkRemovalBusy(false);
    setBulkRemovalOpen(false);
    setBulkRemovalConfirm("");
    setSelectedMemberIds([]);
    reload();
  }

  async function createMember() {
    const email = createEmail.trim().toLowerCase();
    const displayName = createName.trim();
    if (!canManageMembers || !email || !displayName) return;
    if (!MEMBER_EMAIL_RE.test(email)) {
      setCreateError(t("err_create_invalid_email"));
      return;
    }
    if (displayName.length > 120) {
      setCreateError(t("err_create_invalid_name"));
      return;
    }
    setCreateBusy(true);
    setCreateError(null);
    setCreateMessage(null);
    try {
      const member = await createLocalMember({
        email,
        display_name: displayName,
      });
      setCreateEmail("");
      setCreateName("");
      if (member.method === "already_member") {
        setCreateMessage(t("invite_already_member"));
      } else if (member.recovery_path && member.recovery_expires_at) {
        setCreatedCredentials({
          memberId: member.id,
          username: member.email,
          recoveryUrl: new URL(appPath(member.recovery_path), window.location.origin).toString(),
          expiresAt: member.recovery_expires_at,
        });
      }
      reload();
      setOnboardingOpen(false);
    } catch (e) {
      setCreateError(
        e instanceof AdminMembersError && e.status === 422 && e.code === "error"
          ? t("err_create_invalid_fields")
          : formatActionError(e),
      );
    } finally {
      setCreateBusy(false);
    }
  }

  async function resendInvite(inviteId: string) {
    setInviteBusy(true);
    setInviteError(null);
    try {
      const resp = await resendTeamInvite(inviteId);
      if (resp.invite_url) {
        setInviteLink({ url: resp.invite_url, emailSent: Boolean(resp.email_sent) });
      }
      setInviteMessage(t(resp.email_sent ? "invite_resent_email" : "invite_resent_url"));
      reload();
    } catch (e) {
      setInviteError(formatActionError(e));
    } finally {
      setInviteBusy(false);
    }
  }

  async function revokeInvite(inviteId: string) {
    const ok = await confirm({
      title: t("invite_revoke_title"),
      description: t("invite_revoke_confirm"),
      confirmLabel: t("invite_revoke"),
    });
    if (!ok) return;
    setInviteBusy(true);
    setInviteError(null);
    try {
      await revokeTeamInvite(inviteId);
      setInviteMessage(t("invite_revoked"));
      reload();
    } catch (e) {
      setInviteError(formatActionError(e));
    } finally {
      setInviteBusy(false);
    }
  }

  return (
    <div className="settings-people-page">
      <header className="settings-people-hero">
        <div className="settings-people-hero__main">
          <div className="settings-people-hero__eyebrow">
            <Users size={14} />
            {t("eyebrow")}
          </div>
          <h1 className="ui-page-title">{t("title")}</h1>
          <p className="ui-page-desc">{t("subtitle")}</p>
        </div>
        <div className="settings-people-hero__actions">
          {data ? (
            <span className={`people-role-pill${canEditRoles ? " people-role-pill--edit" : ""}`}>
              <Shield size={13} />
              {t("your_role")} <strong>{roleLabel}</strong>
            </span>
          ) : null}
          {canManageMembers && (directCreateAvailable || invitationCreationAvailable) ? (
            <Button variant="primary" onClick={() => {
              setCreateMessage(null);
              setInviteMessage(null);
              setInviteLink(null);
              setOnboardingOpen(true);
            }}>
              <Plus size={13} />{t(directCreateAvailable ? "add_member_title" : "invite_title")}
            </Button>
          ) : null}
        </div>
      </header>

      {error ? (
        <Notice tone="error">
          <div className="people-notice-row">
            <span>{t(hasStaleData ? "refresh_failed_stale" : "load_failed", { err: error })}</span>
            <Button size="sm" onClick={reload}>{t("retry")}</Button>
          </div>
        </Notice>
      ) : null}
      {isReadOnlyByRole ? <Notice tone="muted">{t("read_only_notice")}</Notice> : null}
      {identityError && data ? (
        <Notice tone="muted">
          <div className="people-notice-row">
            <span>{t("identity_failed")}</span>
            <Button size="sm" onClick={reload}>{t("retry")}</Button>
          </div>
        </Notice>
      ) : null}
      {loading && !data ? <LoadingSkeleton rows={5} label={t("loading")} /> : null}
      {data && !authCapabilities && canManageMembers ? (
        <Notice tone="muted">{t("onboarding_mode_unavailable")}</Notice>
      ) : null}
      {createMessage ? <Notice tone="success">{createMessage}</Notice> : null}
      {inviteMessage && !onboardingOpen ? <Notice tone="success">{inviteMessage}</Notice> : null}
      {inviteError && !onboardingOpen ? <Notice tone="error">{t("invite_failed", { err: inviteError })}</Notice> : null}
      {inviteLink && !onboardingOpen ? <InviteLinkBlock link={inviteLink} onDismiss={() => setInviteLink(null)} /> : null}
      {bulkRemovalResult ? <Notice tone={bulkRemovalResult.failed.length ? "error" : "success"}>
        <div>{t("bulk_remove_result", { removed: bulkRemovalResult.removed, failed: bulkRemovalResult.failed.length })}</div>
        {bulkRemovalResult.failed.map((message, index) => <div key={`${index}:${message}`}>{message}</div>)}
      </Notice> : null}

      {data ? (
        <section className="people-list-section">
          <Tabs value={displayView} onValueChange={selectView} idBase="people">
          <TabList appearance="underline" className="people-view-tabs" aria-label={t("view_label")}>
            <Tab value="members" aria-controls="people-panel-members">
              {memberResultsPending ? t("view_members_searching") : memberFilterActive
                ? t("view_members_filtered", { count: memberTotal })
                : t("view_members", { count: memberTotal })}
            </Tab>
            {showInvites ? (
              <Tab value="invites" aria-controls="people-panel-invites">
                {inviteResultsPending ? t("view_invites_searching") : invitesError
                  ? t("view_invites_unavailable") : inviteFilterActive
                    ? t("view_invites_filtered", { count: invitesTotal })
                  : t("view_invites", { count: invitesTotal })}
              </Tab>
            ) : null}
          </TabList>
          </Tabs>
          {displayView === "members" ? (
            <div role="tabpanel" id="people-panel-members" aria-labelledby="people-tab-members" className="people-view-panel">
              <div className="people-list-toolbar">
                <Field id="settings-people-search" label={t("search_label")}>
                  <input id="settings-people-search" className="settings-input" type="search"
                    placeholder={t("search_placeholder")} value={memberSearch} maxLength={120}
                    onChange={(event) => { setSelectedMemberIds([]); setMemberSearch(event.target.value); setMemberPage(1); }} />
                </Field>
                <Field id="settings-people-role" label={t("filter_role")}>
                  <select id="settings-people-role" className="settings-input" value={memberRole}
                    onChange={(event) => { setSelectedMemberIds([]); setMemberRole(event.target.value as TeamRole | ""); setMemberPage(1); }}>
                    <option value="">{t("filter_all_roles")}</option>
                    <option value="owner">{t("role_owner")}</option>
                    <option value="team_admin">{t("role_team_admin")}</option>
                    <option value="audit_admin">{t("role_audit_admin")}</option>
                    <option value="member">{t("role_member")}</option>
                  </select>
                </Field>
                <div className="people-list-toolbar__actions">
                  <PeoplePageSize size={pageSize} onChange={changePageSize} />
                  <Button size="sm" onClick={reload} disabled={loading}>
                    <RefreshCw size={13} />{t("refresh")}
                  </Button>
                </div>
              </div>
              <ActiveFilterChips filters={[
                ...(memberSearch.trim() ? [{ key: "q", label: t("filter_chip_query", { q: memberSearch.trim() }), onRemove: () => { setSelectedMemberIds([]); setMemberSearch(""); setMemberPage(1); } }] : []),
                ...(memberRole ? [{ key: "role", label: t("filter_chip_role", { role: t(`role_${memberRole}`) }), onRemove: () => { setSelectedMemberIds([]); setMemberRole(""); setMemberPage(1); } }] : []),
              ]} onClearAll={() => { setSelectedMemberIds([]); setMemberSearch(""); setMemberRole(""); setMemberPage(1); }} />
              {canDeactivateMembers && selectedMemberIds.length > 0 ? <div className="people-bulk-toolbar">
                <span>{t("selected_count", { count: selectedMemberIds.length })}</span>
                <Button size="sm" variant="ghost" onClick={() => setSelectedMemberIds([])}>{t("clear_selection")}</Button>
                <Button size="sm" onClick={() => { setBulkRemovalConfirm(""); setBulkRemovalOpen(true); }}>
                  <Trash size={13} />{t("bulk_remove_action")}
                </Button>
              </div> : null}
              {!memberResultsPending ? <PeoplePagination position="top" page={memberPage} total={memberTotal} size={pageSize}
                onPageChange={(page) => { setSelectedMemberIds([]); setMemberPage(page); }} /> : null}
              {memberResultsPending ? <Notice tone="muted">{t("searching")}</Notice> : memberTotal === 0 && (debouncedMemberSearch || memberRole) ? (
                <div className="team-members-empty" role="status">{t("search_empty")}</div>
              ) : (
                <MembersTable
                  items={data.items}
                  canEditRoles={canEditRoles}
                  canTransferOwnership={canTransferOwnership}
                  canViewWorkspaceGrants={canViewWorkspaceGrants}
                  canDeactivateMembers={canDeactivateMembers}
                  canRecoverCredentials={canManageMembers}
                  extraActions={(member) => memberDetails.actions(member, canManageMembers && directCreateAvailable)}
                  canViewCredits={canViewCredits}
                  currentUserId={currentUserId}
                  selectedIds={selectedMemberIds}
                  onToggleSelected={(id) => setSelectedMemberIds((current) => current.includes(id)
                    ? current.filter((item) => item !== id) : [...current, id])}
                  onSelectPage={(ids) => setSelectedMemberIds(ids)}
                  onClickPromote={(member) => setTarget(member)}
                  onClickTransferOwnership={(member) => setOwnershipTarget(member)}
                  onClickWorkspaces={(member) => setWorkspaceTarget(member)}
                  onClickDeactivate={(member) => setDeactivateTarget(member)}
                  onClickRecovery={(member) => setRecoveryTarget(member)}
                />
              )}
              {!memberResultsPending ? <PeoplePagination position="bottom" page={memberPage} total={memberTotal} size={pageSize}
                onPageChange={(page) => { setSelectedMemberIds([]); setMemberPage(page); }} /> : null}
            </div>
          ) : (
            <div role="tabpanel" id="people-panel-invites" aria-labelledby="people-tab-invites" className="people-view-panel">
              <div className="people-list-toolbar">
                <Field id="settings-people-invite-search" label={t("search_invite_label")}>
                  <input id="settings-people-invite-search" className="settings-input" type="search"
                    value={inviteSearch} maxLength={120}
                    onChange={(event) => { setInviteSearch(event.target.value); setInvitePage(1); }} />
                </Field>
                <PeoplePageSize size={pageSize} onChange={changePageSize} />
              </div>
              {!inviteResultsPending && !invitesError ? <PeoplePagination position="top" page={invitePage} total={invitesTotal} size={pageSize}
                onPageChange={setInvitePage} /> : null}
              {inviteResultsPending ? <Notice tone="muted">{t("searching")}</Notice> : invitesError ? <Notice tone="error"><div className="people-notice-row">
                <span>{t("invites_load_failed")}</span>
                <Button size="sm" onClick={reload}>{t("retry")}</Button>
              </div></Notice> : <PendingInvites invites={invites} spaces={inviteSpaces} isFiltered={inviteFilterActive} canEdit={canManageMembers}
                busy={inviteBusy} onResend={resendInvite} onRevoke={revokeInvite} />}
              {!inviteResultsPending && !invitesError ? <PeoplePagination position="bottom" page={invitePage} total={invitesTotal} size={pageSize}
                onPageChange={setInvitePage} /> : null}
            </div>
          )}
        </section>
      ) : null}

      {onboardingOpen && (directCreateAvailable || invitationCreationAvailable) ? (
        <Dialog variant="drawer" width={560} title={t(directCreateAvailable ? "add_member_title" : "invite_title")}
          onClose={() => void closeOnboarding()} closeLabel={t("cancel")} busy={createBusy || inviteBusy}
          footer={<div className="people-invite-footer">
            {!directCreateAvailable && !inviteMessage && !batchInviteResults.length ? (
              <div className="people-invite-summary" aria-live="polite">
                <strong>{t("invite_summary_title")}</strong>
                <span>{batchInviteMode ? t("batch_invite_preview", { count: batchEmails.emails.length - batchEmails.invalid.length, duplicates: batchEmails.duplicateCount })
                  : t("invite_summary_email", { email: inviteEmail.trim() || t("invite_summary_email_empty") })}</span>
                <span>{spaceAdminV2 && inviteAssignmentMode === "spaces"
                  ? t("invite_summary_spaces", { count: inviteWorkspaceIds.length, role: t(`workspace_role_${inviteWorkspaceRole}`) })
                  : t("invite_summary_team_only")}</span>
                {spaceAdminV2 && inviteAssignmentMode === "spaces" && !inviteWorkspaceIds.length
                  ? <span className="people-invite-summary__warning">{t("err_invite_space_required")}</span> : null}
              </div>
            ) : null}
            <div className="people-invite-footer__actions">
              <Button onClick={() => void closeOnboarding()} disabled={createBusy || inviteBusy}>{t("cancel")}</Button>
            {directCreateAvailable ? (
              <Button variant="primary" onClick={() => void createMember()}
                disabled={createBusy || !createEmail.trim() || !createName.trim()}>
                {createBusy ? t("create_busy") : t("add_member_submit")}
              </Button>
            ) : (
              <Button variant="primary" onClick={() => void (batchInviteMode ? sendBatchInvites() : sendInvite())}
                disabled={inviteBusy || (batchInviteMode
                  ? !batchEmails.emails.length || batchEmails.emails.length > MAX_BATCH_INVITES
                    || batchEmails.invalid.length > 0 || batchInviteResults.length > 0
                  : !inviteEmail.trim()) || inviteWorkspaceIds.length > 20
                  || (spaceAdminV2 && inviteAssignmentMode === "spaces" && inviteWorkspaceIds.length === 0)}>
                {inviteBusy ? (batchInviteMode
                  ? t("batch_invite_progress", { done: batchInviteProgress, total: batchEmails.emails.length })
                  : t("invite_sending")) : t(batchInviteMode
                  ? batchInviteResults.length ? "batch_invite_done" : "batch_invite_send" : "invite_send")}
              </Button>
            )}
            </div>
          </div>}>
          {directCreateAvailable ? (
            <>
              <p className="people-dialog-copy">{t("add_member_description")}</p>
              <Field id="settings-people-create-name" label={t("create_name_label")}>
                <input id="settings-people-create-name" className="settings-input" value={createName}
                  maxLength={120} onChange={(event) => setCreateName(event.target.value)} />
              </Field>
              <Field id="settings-people-create-email" label={t("create_email_label")}>
                <input id="settings-people-create-email" className="settings-input" type="email" value={createEmail}
                  onChange={(event) => setCreateEmail(event.target.value)} />
              </Field>
              {createError ? <Notice tone="error">{createError}</Notice> : null}
            </>
          ) : (
            <>
              <p className="people-dialog-copy">{t(spaceAdminV2 ? "invite_subtitle_v2" : "invite_subtitle")}</p>
              <SegmentedControl className="people-invite-mode" aria-label={t("invite_input_mode")}
                value={batchInviteMode ? "batch" : "single"} onValueChange={(mode) => setBatchInviteMode(mode === "batch")}>
                <Segment value="single" disabled={batchInviteResults.length > 0}>{t("invite_single")}</Segment>
                <Segment value="batch" disabled={batchInviteResults.length > 0}>{t("invite_batch")}</Segment>
              </SegmentedControl>
              {batchInviteMode ? <>
                <Field id="settings-people-batch-invite" label={t("batch_invite_label")}>
                  <textarea id="settings-people-batch-invite" className="settings-input" rows={6} maxLength={8192}
                    value={batchInviteText} readOnly={batchInviteResults.length > 0}
                    onChange={(event) => { setBatchInviteText(event.target.value); setBatchInviteResults([]); }}
                    placeholder={t("batch_invite_placeholder")} />
                </Field>
                <label className={`people-batch-file${batchInviteResults.length ? " is-disabled" : ""}`}>
                  <Upload size={14} aria-hidden="true" />{t("batch_invite_csv")}
                  <input className="people-batch-file__input" type="file" accept=".csv,text/csv,text/plain"
                    disabled={batchInviteResults.length > 0}
                    onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (!file) return;
                    if (file.size > 8192) { setInviteError(t("batch_invite_file_too_large")); return; }
                    void file.text().then((value) => { setBatchInviteText(value); setInviteError(null); setBatchInviteResults([]); });
                    event.target.value = "";
                  }} />
                </label>
                {batchEmails.invalid.length ? <Notice tone="error">
                  {t("batch_invite_invalid", { emails: batchEmails.invalid.slice(0, 3).join(", ") })}
                </Notice> : null}
                {batchEmails.emails.length > MAX_BATCH_INVITES ? <Notice tone="error">
                  {t("batch_invite_limit", { max: MAX_BATCH_INVITES })}
                </Notice> : null}
              </> : <Field id="settings-people-team-invite" label={t("invite_email_label")}>
                <input id="settings-people-team-invite" className="settings-input" type="email"
                  autoComplete="email" value={inviteEmail} aria-invalid={Boolean(inviteEmailError)}
                  aria-describedby={inviteEmailError ? "settings-people-team-invite-error" : undefined}
                  onChange={(event) => { setInviteEmail(event.target.value); setInviteEmailError(null); }}
                  placeholder={t("invite_email_placeholder")} />
              </Field>}
              {inviteEmailError ? <Notice tone="error"><span id="settings-people-team-invite-error" role="alert">{inviteEmailError}</span></Notice> : null}
              <div className="people-onboarding-team-role">{t("invite_team_role_member")}</div>
              {spaceAdminV2 ? (
                <fieldset className="people-assignment-fieldset">
                  <legend>{t("invite_assignment_title")}</legend>
                  <label className="people-assignment-choice">
                    <input type="radio" name="invite_assignment_mode" checked={inviteAssignmentMode === "spaces"}
                      disabled={inviteSpacesLoading || inviteSpaces.length === 0}
                      onChange={() => setInviteAssignmentMode("spaces")} />
                    {t("invite_assignment_spaces")}
                  </label>
                  <label className="people-assignment-choice">
                    <input type="radio" name="invite_assignment_mode" checked={inviteAssignmentMode === "team_only"}
                      onChange={() => setInviteAssignmentMode("team_only")} />
                    {t("invite_assignment_team_only")}
                  </label>
                  {inviteAssignmentMode === "spaces" ? (
                    <>
                      <Field id="settings-people-invite-space-role" label={t("invite_assignment_role")}>
                        <select id="settings-people-invite-space-role" className="settings-input" value={inviteWorkspaceRole}
                          onChange={(event) => setInviteWorkspaceRole(event.target.value as InitialWorkspaceGrant["role"])}>
                          <option value="member">{t("workspace_role_member")}</option>
                          <option value="viewer">{t("workspace_role_viewer")}</option>
                          <option value="admin">{t("workspace_role_admin")}</option>
                        </select>
                      </Field>
                      <Field id="settings-people-space-search" label={t("space_search_label")}>
                        <input id="settings-people-space-search" className="settings-input" type="search"
                          ref={inviteSpaceSearchRef}
                          value={inviteSpaceSearch} onChange={(event) => setInviteSpaceSearch(event.target.value)} />
                      </Field>
                      <div className="people-space-selection-meta">
                        <span>{t("space_selected_count", { count: inviteWorkspaceIds.length })}</span>
                        {!inviteSpacesLoading && !inviteSpacesError
                          ? <span>{t("space_options_count", { count: filteredInviteSpaces.length })}</span> : null}
                      </div>
                      {inviteWorkspaceIds.length ? (
                        <div className="people-selected-spaces" role="group" aria-label={t("space_selected_label")}>
                          {inviteWorkspaceIds.map((id) => {
                            const name = inviteSpaces.find((space) => space.workspace_id === id)?.name ?? id;
                            return <button key={id} type="button" aria-label={t("space_remove_selected", { name })}
                              onClick={() => {
                                setInviteWorkspaceIds((current) => current.filter((item) => item !== id));
                                inviteSpaceSearchRef.current?.focus();
                              }}>
                              <span>{name}</span><X size={12} aria-hidden="true" />
                            </button>;
                          })}
                        </div>
                      ) : null}
                      <div className="people-space-options">
                        {filteredInviteSpaces.map((space) => (
                          <label key={space.workspace_id}>
                            <input type="checkbox" checked={inviteWorkspaceIds.includes(space.workspace_id)}
                              disabled={inviteWorkspaceIds.length >= 20 && !inviteWorkspaceIds.includes(space.workspace_id)}
                              onChange={(event) => setInviteWorkspaceIds((current) => event.target.checked
                                ? [...current, space.workspace_id] : current.filter((id) => id !== space.workspace_id))} />
                            <span>{space.name}</span>
                          </label>
                        ))}
                        {inviteSpaceSearch.trim() && !inviteSpacesLoading && !inviteSpacesError
                          && !filteredInviteSpaces.length ? <div className="people-space-options__empty" role="status">
                          {t("space_search_empty")}
                        </div> : null}
                      </div>
                      {!inviteWorkspaceIds.length ? <p className="people-form-hint">{t("err_invite_space_required")}</p> : null}
                    </>
                  ) : null}
                  {inviteAssignmentMode === "team_only" ? <p className="people-form-hint">{t("invite_assignment_team_only_warning")}</p> : null}
                  {inviteSpacesLoading ? <p className="people-form-hint">{t("workspace_loading")}</p> : null}
                  {inviteSpacesError ? <Notice tone="error">{t("invite_space_load_failed")}</Notice> : null}
                </fieldset>
              ) : null}
              {inviteError ? <Notice tone="error">{t("invite_failed", { err: inviteError })}</Notice> : null}
              {inviteMessage ? <Notice tone="success">{inviteMessage}</Notice> : null}
              {inviteLink ? <InviteLinkBlock link={inviteLink} onDismiss={() => setInviteLink(null)} /> : null}
              {batchInviteResults.length ? <div className="people-batch-results" role="status">
                <strong>{t("batch_invite_results", { count: batchInviteResults.length })}</strong>
                {batchInviteResults.map((item) => <div key={item.email}>
                  <span>{item.email}</span><span>{item.result}</span>
                  {item.url ? <Button size="sm" onClick={() => {
                    void copyTextWithFallback(item.url!, t("invite_link_copy_prompt")).then((copied) => {
                      if (copied) setCopiedBatchInviteEmails((current) => [...current, item.email]);
                    });
                  }}>
                    {copiedBatchInviteEmails.includes(item.email) ? t("invite_link_copied") : t("invite_link_copy")}
                  </Button> : null}
                </div>)}
              </div> : null}
            </>
          )}
        </Dialog>
      ) : null}

      {onboardingDiscardOpen ? <ConfirmDialog
        title={t("onboarding_discard_title")}
        description={t("onboarding_discard")}
        confirmLabel={t("onboarding_discard_confirm")}
        cancelLabel={t("cancel")}
        onCancel={() => setOnboardingDiscardOpen(false)}
        onConfirm={discardOnboarding}
      /> : null}

      {target && data ? (
        <PromoteModal
          member={target}
          currentRole={target.team_role}
          isOwner={target.is_owner}
          isSelf={target.user_id === currentUserId}
          onlyRemainingTeamAdmin={
            target.team_role === "team_admin" && nonOwnerTeamAdminCount <= 1
          }
          onClose={() => setTarget(null)}
          onSuccess={() => {
            setTarget(null);
            reload();
          }}
        />
      ) : null}

      {ownershipTarget ? (
        <TransferOwnerDialog
          member={ownershipTarget}
          onClose={() => setOwnershipTarget(null)}
          onSuccess={() => {
            setOwnershipTarget(null);
            reload();
          }}
        />
      ) : null}

      {workspaceTarget ? (
        <WorkspaceGrantsDialog
          member={workspaceTarget}
          canManage={canManageWorkspaceGrants}
          formatActionError={formatActionError}
          onClose={() => setWorkspaceTarget(null)}
        />
      ) : null}

      {deactivateTarget && authCapabilities ? (
        <DeactivateMemberDialog
          member={deactivateTarget}
          deploymentMode={authCapabilities.deployment_mode}
          formatActionError={formatActionError}
          onClose={() => setDeactivateTarget(null)}
          onSuccess={() => {
            setDeactivateTarget(null);
            reload();
          }}
        />
      ) : null}

      {bulkRemovalOpen && authCapabilities ? <Dialog
        title={t("bulk_remove_title", { count: selectedMembers.length })}
        onClose={() => { if (!bulkRemovalBusy) setBulkRemovalOpen(false); }}
        busy={bulkRemovalBusy}
        footer={<>
          <Button onClick={() => setBulkRemovalOpen(false)} disabled={bulkRemovalBusy}>{t("cancel")}</Button>
          <Button variant="danger" onClick={() => void removeSelectedMembers()}
            disabled={bulkRemovalBusy || bulkRemovalConfirm !== String(selectedMembers.length)}>
            {bulkRemovalBusy ? t("bulk_remove_busy") : t("bulk_remove_confirm")}
          </Button>
        </>}
      >
        <Notice tone="muted">{t(authCapabilities.deployment_mode === "local"
          ? "bulk_remove_local_effect" : "bulk_remove_saas_effect")}</Notice>
        <ul className="people-bulk-preview">
          {selectedMembers.map((member) => <li key={member.user_id}>{member.display_name || member.email} · {member.email}</li>)}
        </ul>
        <Field id="people-bulk-remove-confirm" label={t("bulk_remove_count_prompt", { count: selectedMembers.length })}>
          <input id="people-bulk-remove-confirm" className="settings-input" inputMode="numeric"
            value={bulkRemovalConfirm} onChange={(event) => setBulkRemovalConfirm(event.target.value)} />
        </Field>
      </Dialog> : null}

      {recoveryTarget ? (
        <CredentialRecoveryDialog
          member={recoveryTarget}
          formatActionError={formatActionError}
          onClose={() => setRecoveryTarget(null)}
        />
      ) : null}

      {memberDetails.dialog}

      {createdCredentials ? (
        <CreatedCredentialsDialog
          credentials={createdCredentials}
          onClose={() => setCreatedCredentials(null)}
          onManageSpaces={() => {
            setWorkspaceTarget({ user_id: createdCredentials.memberId, email: createdCredentials.username, display_name: null });
            setCreatedCredentials(null);
          }}
        />
      ) : null}
      {confirmDialog}
    </div>
  );
}

function PeoplePageSize({ size, onChange }: { size: number; onChange: (size: number) => void }) {
  const t = useTranslations("settings.people");
  return <label className="people-page-size">
    <span>{t("page_size_label")}</span>
    <select className="settings-input" value={size} onChange={(event) => onChange(Number(event.target.value))}>
      {PAGE_SIZE_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
    </select>
  </label>;
}

function PeoplePagination({
  position,
  page,
  total,
  size,
  onPageChange,
}: {
  position: "top" | "bottom";
  page: number;
  total: number;
  size: number;
  onPageChange: (page: number) => void;
}) {
  const t = useTranslations("settings.people");
  const pages = Math.max(1, Math.ceil(total / size));
  if (pages <= 1) return null;
  return <nav className="people-pagination" aria-label={t(position === "top" ? "pagination_top_label" : "pagination_label")}>
    <span>{t("pagination_status", { page, pages, total })}</span>
    <div>
      <Button size="sm" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>{t("previous_page")}</Button>
      <Button size="sm" disabled={page >= pages} onClick={() => onPageChange(page + 1)}>{t("next_page")}</Button>
    </div>
  </nav>;
}

function CreatedCredentialsDialog({
  credentials,
  onClose,
  onManageSpaces,
}: {
  credentials: { memberId: string; username: string; recoveryUrl: string; expiresAt: string };
  onClose: () => void;
  onManageSpaces: () => void;
}) {
  const t = useTranslations("settings.people");
  const [copied, setCopied] = useState<"username" | "recovery" | null>(null);
  const { confirm, dialog: confirmDialog } = useConfirmDialog();

  async function leaveWithoutCopy(): Promise<boolean> {
    if (copied === "recovery") return true;
    return confirm({
      title: t("local_link_not_copied_title"),
      description: t("local_link_not_copied"),
      confirmLabel: t("close_anyway"),
      cancelLabel: t("back_to_copy"),
    });
  }

  async function close() {
    if (!(await leaveWithoutCopy())) return;
    onClose();
  }

  async function manageSpaces() {
    if (!(await leaveWithoutCopy())) return;
    onManageSpaces();
  }

  function copyCredential(kind: "username" | "recovery", value: string) {
    const prompt = kind === "recovery"
      ? t("invite_local_copy_recovery_link")
      : t("create_credentials_username_copy_prompt");
    void copyTextWithFallback(value, prompt).then((ok) => {
      if (ok) setCopied(kind);
    });
  }

  return (
    <Dialog
      title={t("invite_local_credentials_title")}
      onClose={() => void close()}
      footer={<>
        <Button onClick={() => void close()}>{t("create_credentials_close")}</Button>
        <Button variant="primary" onClick={() => void manageSpaces()}>{t("manage_created_spaces")}</Button>
      </>}
    >
      {confirmDialog}
      <Notice tone="success">{t("invite_local_credentials_notice")}</Notice>
      <div className="people-created-credentials">
        <div className="people-credential-row">
          <div>
            <span>{t("create_credentials_username")}</span>
            <code>{credentials.username}</code>
          </div>
          <Button
            size="sm"
            onClick={() => copyCredential("username", credentials.username)}
          >
            {copied === "username" ? t("create_credentials_copied") : t("create_credentials_copy_username")}
          </Button>
        </div>
        <div className="people-credential-row">
          <div>
            <span>{t("invite_local_recovery_link")}</span>
            <code>{credentials.recoveryUrl}</code>
          </div>
          <Button
            size="sm"
            onClick={() => copyCredential("recovery", credentials.recoveryUrl)}
          >
            {copied === "recovery" ? t("create_credentials_copied") : t("invite_local_copy_recovery_link")}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

function InviteLinkBlock({
  link,
  onDismiss,
}: {
  link: { url: string; emailSent: boolean };
  onDismiss: () => void;
}) {
  const t = useTranslations("settings.people");
  const [copied, setCopied] = useState(false);
  return (
    <div className="people-invite-link">
      <div>
        <strong>{link.emailSent ? t("invite_link_email_sent") : t("invite_link_url_only")}</strong>
        <code>{link.url}</code>
      </div>
      <div className="people-invite-link__actions">
        <Button
          size="sm"
          onClick={() => {
            void copyTextWithFallback(link.url, t("invite_link_copy_prompt")).then((ok) => {
              if (ok) setCopied(true);
            });
          }}
        >
          {copied ? t("invite_link_copied") : t("invite_link_copy")}
        </Button>
        <Button size="sm" variant="ghost" onClick={onDismiss}>{t("invite_link_dismiss")}</Button>
      </div>
    </div>
  );
}

function PendingInvites({
  invites,
  spaces,
  isFiltered,
  canEdit,
  busy,
  onResend,
  onRevoke,
}: {
  invites: TeamInviteRow[];
  spaces: TeamSpaceRow[];
  isFiltered: boolean;
  canEdit: boolean;
  busy: boolean;
  onResend: (inviteId: string) => Promise<void>;
  onRevoke: (inviteId: string) => Promise<void>;
}) {
  const t = useTranslations("settings.people");
  if (invites.length === 0) {
    return <div className="people-pending-empty" role="status">{t(isFiltered ? "pending_search_empty" : "pending_empty")}</div>;
  }
  return (
    <div className="people-pending-list" aria-label={t("pending_title")}>
      <div className="people-pending-list__title">{t("pending_title")}</div>
      {invites.map((invite) => (
        <div className="people-pending-row" key={invite.id}>
          <div>
            <strong>{invite.invited_email}</strong>
            <span>{t("pending_expires", { date: invite.expires_at.slice(0, 10) })}</span>
            {invite.assignment_mode ? (
              <span>
                {invite.assignment_mode === "spaces"
                  ? t("pending_assignment_count", { count: invite.initial_workspace_grants?.length ?? 0 })
                  : t("pending_assignment_team_only")}
              </span>
            ) : null}
            {invite.assignment_mode === "spaces" && invite.initial_workspace_grants?.length ? (
              <details className="people-pending-spaces">
                <summary>{t("pending_assignment_show")}</summary>
                <span>{invite.initial_workspace_grants.map((grant) =>
                  spaces.find((space) => space.workspace_id === grant.workspace_id)?.name ?? t("pending_assignment_unknown"),
                ).join("、")}</span>
              </details>
            ) : null}
          </div>
          {canEdit ? (
            <div className="people-pending-row__actions">
              <RowActionsMenu
                label={t("pending_more_actions", { email: invite.invited_email })}
                pending={busy}
                actions={[
                  { id: "resend", label: t("invite_resend"), disabled: busy, onSelect: () => void onResend(invite.id) },
                  { id: "revoke", label: t("invite_revoke"), danger: true, disabled: busy, onSelect: () => void onRevoke(invite.id) },
                ]}
              />
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function WorkspaceGrantsDialog({
  member,
  canManage,
  formatActionError,
  onClose,
}: {
  member: Pick<AdminMemberRow, "user_id" | "display_name" | "email">;
  canManage: boolean;
  formatActionError: (e: unknown) => string;
  onClose: () => void;
}) {
  const t = useTranslations("settings.people");
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  const [items, setItems] = useState<WorkspaceGrantRow[]>([]);
  const [grantDrafts, setGrantDrafts] = useState<Record<string, "admin" | "member" | "viewer">>({});
  const [grantedSearch, setGrantedSearch] = useState("");
  const [availableSearch, setAvailableSearch] = useState("");
  const [showAvailable, setShowAvailable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await fetchMemberWorkspaceGrants(member.user_id);
      setItems(body.items);
    } catch (e) {
      setError(formatActionError(e));
    } finally {
      setLoading(false);
    }
  }, [formatActionError, member.user_id]);

  useEffect(() => { void reload(); }, [reload]);

  function formatWorkspaceError(e: unknown): string {
    if (e instanceof AdminMembersError && e.status === 422 && e.code === "error") {
      return t("err_workspace_invalid_fields");
    }
    return formatActionError(e);
  }

  async function setGrant(item: WorkspaceGrantRow, role: "admin" | "member" | "viewer") {
    if (!canManage) return;
    setBusyKey(item.workspace_id);
    setError(null);
    setSuccess(null);
    try {
      await upsertMemberWorkspaceGrant(member.user_id, item.workspace_id, role);
      setGrantDrafts((prev) => {
        const next = { ...prev };
        delete next[item.workspace_id];
        return next;
      });
      await reload();
      setSuccess(t("workspace_saved", { name: item.name, role: t(`workspace_role_${role}`) }));
    } catch (e) {
      setError(formatWorkspaceError(e));
    } finally {
      setBusyKey(null);
    }
  }

  async function revokeGrant(item: WorkspaceGrantRow) {
    if (!canManage) return;
    const ok = await confirm({
      title: t("workspace_revoke_title", { name: item.name }),
      description: t("workspace_revoke_confirm"),
      confirmLabel: t("workspace_revoke"),
    });
    if (!ok) return;
    setBusyKey(item.workspace_id);
    setError(null);
    setSuccess(null);
    try {
      await revokeMemberWorkspaceGrant(member.user_id, item.workspace_id);
      await reload();
      setSuccess(t("workspace_revoked", { name: item.name }));
    } catch (e) {
      setError(formatWorkspaceError(e));
    } finally {
      setBusyKey(null);
    }
  }

  const granted = items.filter((item) => item.role);
  const available = items.filter((item) => !item.role);
  const shownGranted = granted.filter((item) => item.name.toLowerCase().includes(grantedSearch.trim().toLowerCase()));
  const shownAvailable = available.filter((item) => item.name.toLowerCase().includes(availableSearch.trim().toLowerCase()));
  const hasUnsavedRole = Object.entries(grantDrafts).some(([workspaceId, role]) =>
    role !== (items.find((item) => item.workspace_id === workspaceId)?.role ?? "member"),
  );
  const closeWithGuard = () => {
    if (busyKey) return;
    if (hasUnsavedRole) setConfirmDiscard(true);
    else onClose();
  };

  return (
    <>
    <Dialog title={t("workspace_dialog_title", { name: member.display_name || member.email })} onClose={closeWithGuard}
      width={720} busy={Boolean(busyKey)} footer={<Button onClick={closeWithGuard} disabled={Boolean(busyKey)}>{t("workspace_close")}</Button>}>
      {!canManage ? <Notice tone="muted">{t("workspace_read_only")}</Notice> : null}
      {error ? <Notice tone="error">{t("workspace_action_failed", { err: error })}</Notice> : null}
      {success ? <Notice tone="success">{success}</Notice> : null}
      {loading && items.length === 0 ? <Notice tone="muted">{t("workspace_loading")}</Notice> : null}
      {!loading && items.length === 0 ? <div className="people-pending-empty">{t("workspace_empty")}</div> : null}
      {items.length > 0 ? (
        <>
          <div className="people-workspace-section-head">
            <strong>{t("workspace_granted_title", { count: granted.length })}</strong>
            <Button size="sm" onClick={() => setShowAvailable((value) => !value)}>
              {t(showAvailable ? "workspace_add_close" : canManage ? "workspace_add_open" : "workspace_show_available")}
            </Button>
          </div>
          <Field id="people-granted-search" label={t("workspace_search_granted")}>
            <input id="people-granted-search" className="settings-input" type="search" value={grantedSearch}
              onChange={(event) => setGrantedSearch(event.target.value)} />
          </Field>
          <div className="people-workspace-list">
            {shownGranted.length === 0 ? <div className="people-pending-empty">{t(granted.length ? "workspace_search_empty" : "workspace_no_grants")}</div> : null}
            {shownGranted.map((item) => {
              const busy = busyKey === item.workspace_id;
              const role = grantDrafts[item.workspace_id] ?? item.role ?? "member";
              return <div className="people-workspace-row" key={item.workspace_id}>
                <div>
                  <strong>{item.name}</strong>
                  <details className="people-workspace-id"><summary>{t("workspace_id_label")}</summary><code>{item.workspace_id}</code></details>
                </div>
                <div className="people-workspace-row__actions">
                  {canManage ? <>
                    <select className="settings-select" value={role} disabled={busy}
                      aria-label={t("workspace_role_for", { name: item.name })}
                      onChange={(event) => setGrantDrafts((prev) => ({ ...prev,
                        [item.workspace_id]: event.target.value as "admin" | "member" | "viewer" }))}>
                      <option value="viewer">{t("workspace_role_viewer")}</option>
                      <option value="member">{t("workspace_role_member")}</option>
                      <option value="admin">{t("workspace_role_admin")}</option>
                    </select>
                    {role !== item.role ? <Button size="sm" onClick={() => void setGrant(item, role)} disabled={busy}>
                      {t("workspace_save_role")}
                    </Button> : null}
                    <Button size="sm" variant="ghost" onClick={() => void revokeGrant(item)} disabled={busy}>
                      {t("workspace_revoke")}
                    </Button>
                  </> : <span className="team-role-badge team-role-badge--member">{t(`workspace_role_${role}`)}</span>}
                </div>
              </div>;
            })}
          </div>
          {showAvailable ? <div className="people-available-spaces">
            <div className="people-workspace-section-head"><strong>{t("workspace_available_title", { count: available.length })}</strong></div>
            <Field id="people-available-search" label={t("workspace_search_available")}>
              <input id="people-available-search" className="settings-input" type="search" value={availableSearch}
                onChange={(event) => setAvailableSearch(event.target.value)} />
            </Field>
            <div className="people-workspace-list">
              {shownAvailable.length === 0 ? <div className="people-pending-empty">{t("workspace_search_empty")}</div> : null}
              {shownAvailable.map((item) => {
                const busy = busyKey === item.workspace_id;
                const role = grantDrafts[item.workspace_id] ?? "member";
                return <div className="people-workspace-row" key={item.workspace_id}>
                  <div><strong>{item.name}</strong></div>
                  <div className="people-workspace-row__actions">
                    {canManage ? <select className="settings-select" value={role} disabled={busy}
                      aria-label={t("workspace_role_for", { name: item.name })}
                      onChange={(event) => setGrantDrafts((prev) => ({ ...prev,
                        [item.workspace_id]: event.target.value as "admin" | "member" | "viewer" }))}>
                      <option value="viewer">{t("workspace_role_viewer")}</option>
                      <option value="member">{t("workspace_role_member")}</option>
                      <option value="admin">{t("workspace_role_admin")}</option>
                    </select> : <span className="team-role-badge team-role-badge--member">{t("workspace_no_grant")}</span>}
                    {canManage ? <Button size="sm" onClick={() => void setGrant(item, role)} disabled={busy}>
                      <Folder size={12} />{t("workspace_grant")}
                    </Button> : null}
                  </div>
                </div>;
              })}
            </div>
          </div> : null}
        </>
      ) : null}
    </Dialog>
    {confirmDiscard ? <ConfirmDialog
      title={t("workspace_discard_title")}
      description={t("workspace_discard_description")}
      confirmLabel={t("workspace_discard_confirm")}
      cancelLabel={t("cancel")}
      onCancel={() => setConfirmDiscard(false)}
      onConfirm={() => { setConfirmDiscard(false); onClose(); }}
    /> : null}
    {confirmDialog}
    </>
  );
}

function DeactivateMemberDialog({
  member,
  deploymentMode,
  formatActionError,
  onClose,
  onSuccess,
}: {
  member: AdminMemberRow;
  deploymentMode: "local" | "saas";
  formatActionError: (e: unknown) => string;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const t = useTranslations("settings.people");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await deactivateTeamMember(member.user_id);
      onSuccess();
    } catch (e) {
      setError(formatActionError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={t(deploymentMode === "local" ? "deactivate_local_title" : "deactivate_dialog_title")}
      onClose={onClose}
      busy={busy}
      footer={(
        <>
          <Button onClick={onClose} disabled={busy}>{t("cancel")}</Button>
          <Button variant="danger" onClick={() => void submit()} disabled={busy}>
            <Trash size={13} />
            {busy ? t("deactivate_busy") : t("deactivate_confirm")}
          </Button>
        </>
      )}
    >
      <p className="people-dialog-copy">
        {t(deploymentMode === "local" ? "deactivate_local_body" : "deactivate_dialog_body", { name: member.display_name || member.email })}
      </p>
      {error ? <Notice tone="error">{t("deactivate_failed", { err: error })}</Notice> : null}
    </Dialog>
  );
}

function CredentialRecoveryDialog({
  member,
  formatActionError,
  onClose,
}: {
  member: AdminMemberRow;
  formatActionError: (e: unknown) => string;
  onClose: () => void;
}) {
  const t = useTranslations("settings.people");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recovery, setRecovery] = useState<{ url: string; expiresAt: string } | null>(null);
  const [copied, setCopied] = useState(false);

  async function generate() {
    setBusy(true);
    setError(null);
    setRecovery(null);
    try {
      const result = await createCredentialRecovery(member.user_id);
      setRecovery({ url: result.recovery_url, expiresAt: result.expires_at });
    } catch (caught) {
      setError(formatActionError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    setBusy(true);
    setError(null);
    try {
      await revokeCredentialRecovery(member.user_id);
      setRecovery(null);
    } catch (caught) {
      setError(formatActionError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={t("recovery_dialog_title")}
      onClose={onClose}
      busy={busy}
      footer={(
        <>
          <Button onClick={onClose} disabled={busy}>{t("cancel")}</Button>
          {recovery ? (
            <Button variant="ghost" onClick={() => void revoke()} disabled={busy}>
              {t("recovery_revoke")}
            </Button>
          ) : (
            <Button variant="primary" onClick={() => void generate()} disabled={busy}>
              {busy ? t("recovery_busy") : t("recovery_generate")}
            </Button>
          )}
        </>
      )}
    >
      <p className="people-dialog-copy">
        {t("recovery_dialog_body", { name: member.display_name || member.email })}
      </p>
      {recovery ? (
        <div className="people-invite-link">
          <div>
            <strong>{t("recovery_reveal_once")}</strong>
            <code>{recovery.url}</code>
            <span>{t("recovery_expires", { date: recovery.expiresAt })}</span>
          </div>
          <Button
            size="sm"
            onClick={() => {
              void copyTextWithFallback(recovery.url, t("recovery_copy_prompt")).then((ok) => {
                if (ok) setCopied(true);
              });
            }}
          >
            {copied ? t("invite_link_copied") : t("recovery_copy")}
          </Button>
        </div>
      ) : null}
      {error ? <Notice tone="error">{t("recovery_failed", { err: error })}</Notice> : null}
    </Dialog>
  );
}

import type { AdminMemberRow } from "@/lib/admin-members-api";
import type { SessionMenuAction } from "@/components/sidebar/session-action-menu";
export function useEditionMemberDetails() {
  return {
    actions(_member: AdminMemberRow, _allowed: boolean): SessionMenuAction[] {
      return [];
    },
    dialog: null,
  };
}

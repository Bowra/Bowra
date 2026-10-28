import { describe, expect, it } from "vitest";

import en from "@/messages/en-US.json";

// Reviewed static menu labels. User-authored names, provider names, descriptions,
// notices and disabled reasons retain their own spelling and sentence case.
const MENU_LABELS = [
  "files_page.sort.created_desc",
  "files_page.sort.last_used_desc",
  "files_page.ops.purge",
  "files_page.use_in_chat",
  "reports_page.sort_updated",
  "reports_page.sort_longest",
  "reports_page.copy_perm_link",
  "settings.datarc.rotate",
  "export_ui.pdf_name",
  "export_ui.docx_name",
  "export_ui.pptx_name",
  "export_ui.xlsx_name",
  "export_ui.past_exports",
  "settings.subagents.builtin.action_reset",
  "chat.msg_eval_unshare",
  "chat.msg_eval_share",
  "settings.public_entries.reissue",
  "settings.public_entries.make_default",
  "agents_marketplace.eval_suite_rename",
  "agents_marketplace.role_preference_clear",
  "agents_marketplace.eval_point_check_on_answers",
  "agents_marketplace.eval_point_view_calibration",
  "agents_marketplace.eval_point_hide_calibration",
  "agents_marketplace.eval_case_remove",
  "agents_marketplace.eval_case_edit",
  "admin.members_table.change_role",
  "knowledge_base.filter.sort_field",
  "knowledge_base.filter.clear_status",
  "knowledge_base.filter.clear_type",
  "knowledge_base.filter.date.all",
  "knowledge_base.filter.date.d7",
  "knowledge_base.filter.date.d30",
  "knowledge_base.filter.date.d90",
  "knowledge_base.filter.sort.uploadedAt",
  "knowledge_base.filter.sort_order.uploadedAt.desc",
  "knowledge_base.filter.sort_order.uploadedAt.asc",
  "knowledge_base.filter.sort_order.sizeMB.desc",
  "knowledge_base.filter.sort_order.sizeMB.asc",
  "knowledge_base.manager.row_menu.details",
  "knowledge_base.manager.row_menu.bind_doctype",
  "knowledge_base.manager.row_menu.change_scope",
  "knowledge_base.manager.row_menu.reparse",
  "canvas.edit_this_report",
  "canvas.hdr_copy_perm_link",
  "canvas.hdr_clear_report",
  "canvas.hdr_delete_report",
  "kbMatrix.runmenu_all",
  "kbMatrix.runmenu_stale",
  "kbMatrix.runmenu_force",
  "kbMatrix.colmenu_run",
  "kbMatrix.colmenu_force",
  "kbMatrix.colmenu_edit",
  "kbMatrix.colmenu_delete",
  "knowledge_base.nav_v2.view_records",
  "chart_block.open_data",
  "chart_table.open_data",
  "artifact_viewer.view_raw",
  "artifact_viewer.view_details",
  "artifact_viewer.version_history",
  "artifact_viewer.feedback_unsupported",
  "model_picker.clear",
  "memory_page.v2.reset_permanent_delete_action",
  "memory_page.v2.permanent_delete",
  "memory_page.v2.delete",
  "composer.upload_file",
  "section_chrome.menu_merge_prev",
  "section_chrome.menu_split",
  "section_chrome.menu_move_up",
  "section_chrome.menu_move_down",
  "report_status.action_verify",
  "report_status.action_unverify",
  "admin.credits.row_action_edit",
  "admin.credits.row_action_grant",
  "admin.credits.row_action_drilldown",
  "chart_block.kind.hbar",
  "chart_block.kind.stacked_bar",
  "chart_block.kind.dual_axis",
  "chart_block.kind.combo",
  "chart_block.kind.area_stacked",
  "chart_block.kind.geo",
  "admin.credits.row_action_reset_user_menu",
  "locale.answer_follow_menu",
  "agents_marketplace.menu_taxonomy_domain_finance",
  "agents_marketplace.menu_taxonomy_domain_marketing",
  "agents_marketplace.menu_taxonomy_domain_sales",
  "agents_marketplace.menu_taxonomy_domain_operations",
  "agents_marketplace.menu_taxonomy_domain_people",
  "agents_marketplace.menu_taxonomy_domain_it_data",
  "agents_marketplace.menu_taxonomy_domain_general",
  "sidebar.user_menu.release_notes",
  "sidebar.user_menu.version_copy_hint",
  "sidebar.answer_language_link",
  "chat_card.copy_diagnostics_menu",
] as const;

function message(path: string): string {
  let value: unknown = en;
  for (const key of path.split(".")) {
    value = (value as Record<string, unknown>)[key];
  }
  if (typeof value !== "string") throw new Error(`Missing menu label: ${path}`);
  return value;
}

const SMALL_WORDS = new Set([
  "a",
  "an",
  "the",
  "and",
  "or",
  "but",
  "for",
  "nor",
  "as",
  "at",
  "by",
  "in",
  "of",
  "on",
  "per",
  "to",
  "via",
  "vs",
  "with",
  "from",
  "into",
  "onto",
]);

describe("English menu copy", () => {
  it.each(MENU_LABELS)("uses Title Case for %s", (path) => {
    const words =
      message(path)
        .replace(/\{[^}]*\}/g, "")
        .match(/[A-Za-z][A-Za-z'-]*/g) ?? [];
    expect(words.length).toBeGreaterThan(0);
    words.forEach((word, index) => {
      if (index > 0 && index < words.length - 1 && SMALL_WORDS.has(word))
        return;
      expect(word[0], `${path}: ${word} must start with a capital letter`).toBe(
        word[0].toUpperCase(),
      );
    });
  });

  it("keeps separate sentence-case copy for the answer-language value and balance-reset dialog", () => {
    expect(message("locale.answer_follow")).toBe("Match my question");
    expect(message("admin.credits.row_action_reset_user")).toBe(
      "Reset this user's balance",
    );
    expect(message("agents_marketplace.taxonomy_domain_people")).toBe(
      "People & organization",
    );
  });
});

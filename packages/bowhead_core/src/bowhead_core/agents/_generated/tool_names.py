# ⚠️ AUTO-GENERATED — DO NOT EDIT BY HAND ⚠️
#
# Source: packages/bowhead_core/src/bowhead_core/agents/manifests/tool_aliases.yaml
# Regenerate: uv run python -m bowhead_cli.codegen_tool_aliases
# CI check  : uv run python -m bowhead_cli.codegen_tool_aliases --check

"""Python constants for tool names. Import these instead of literals."""

from __future__ import annotations

ADD_CHART = "add_chart"
ADD_IMAGE = "add_image"
AGENT_AUTHORING_INSPECT = "agent_authoring_inspect"
AGENT_AUTHORING_PROPOSAL_GET = "agent_authoring_proposal_get"
AGENT_AUTHORING_PROPOSE = "agent_authoring_propose"
AGENT_AUTHORING_TOOLS = "agent_authoring_tools"
AGENT_AUTHORING_VALIDATE = "agent_authoring_validate"
AGENT_CHANGE_GET = "agent_change_get"
AGENT_CHANGE_PROPOSE = "agent_change_propose"
AGENT_CHANGE_VALIDATE = "agent_change_validate"
AGENT_CI_CANCEL = "agent_ci_cancel"
AGENT_CI_GET = "agent_ci_get"
AGENT_CI_PREPARE = "agent_ci_prepare"
AGENT_CI_REQUEST = "agent_ci_request"
AGENT_CI_START = "agent_ci_start"
AGENT_DRAFT_START = "agent_draft_start"
AGENT_EVAL_ALIGNMENT_ASK = "agent_eval_alignment_ask"
AGENT_EVAL_ALIGNMENT_GET = "agent_eval_alignment_get"
AGENT_EVAL_ALIGNMENT_REPORT = "agent_eval_alignment_report"
AGENT_EVAL_ALIGNMENT_START = "agent_eval_alignment_start"
AGENT_EVAL_CASE_PROPOSAL_GET = "agent_eval_case_proposal_get"
AGENT_EVAL_CASE_PROPOSE = "agent_eval_case_propose"
AGENT_EVAL_EVIDENCE_GET = "agent_eval_evidence_get"
AGENT_EVAL_SUITE_GET = "agent_eval_suite_get"
AGENT_EVAL_SUITE_LIST = "agent_eval_suite_list"
AGENT_EVAL_TURNS_LIST = "agent_eval_turns_list"
AGENT_OPERATION_GET = "agent_operation_get"
AGENT_OPERATION_OPEN = "agent_operation_open"
AGENT_PLUGIN_INSPECT = "agent_plugin_inspect"
AGENT_PLUGIN_OPERATION_GET = "agent_plugin_operation_get"
AGENT_PLUGIN_SAVE = "agent_plugin_save"
AGENT_PLUGIN_VALIDATE = "agent_plugin_validate"
AGENT_RESOURCE_GET = "agent_resource_get"
AGENT_RESOURCE_OPEN = "agent_resource_open"
AGENT_RESOURCE_SEARCH = "agent_resource_search"
AGENT_SKILL_CANCEL = "agent_skill_cancel"
AGENT_SKILL_INSPECT = "agent_skill_inspect"
AGENT_SKILL_OPERATION_GET = "agent_skill_operation_get"
AGENT_SKILL_READ = "agent_skill_read"
AGENT_SKILL_SAVE = "agent_skill_save"
AGENT_SKILL_VALIDATE = "agent_skill_validate"
AGENT_TOOL_REQUIREMENT_CREATE = "agent_tool_requirement_create"
AGENT_TOOL_REQUIREMENT_GET = "agent_tool_requirement_get"
AGENT_TOOL_REQUIREMENT_OPEN = "agent_tool_requirement_open"
AGENT_TOOL_REQUIREMENT_RECONCILE = "agent_tool_requirement_reconcile"
AGENT_WORKING_COPY_OPEN = "agent_working_copy_open"
ATTACHMENT_READ = "attachment_read"
BI_CONCEPT_RESOLVE = "bi_concept_resolve"
BI_QUERY = "bi_query"
BI_SCHEMA = "bi_schema"
CANVAS_ADD_EXTERNAL_IFRAME = "canvas_add_external_iframe"
CANVAS_ADD_HTML_ARTIFACT = "canvas_add_html_artifact"
CANVAS_ADD_INSIGHT = "canvas_add_insight"
CANVAS_ADD_SPEC_BLOCK = "canvas_add_spec_block"
CANVAS_CREATE_DESIGN = "canvas_create_design"
CANVAS_FLATTEN_DESIGN = "canvas_flatten_design"
CANVAS_UPDATE_DESIGN_TEXT = "canvas_update_design_text"
CONFIGURATION_APPLY = "configuration_apply"
CONFIGURATION_LIST = "configuration_list"
CONFIGURATION_OPEN = "configuration_open"
CONFIGURATION_PROPOSE = "configuration_propose"
CONFIGURATION_READ = "configuration_read"
CONFIGURATION_RESULT = "configuration_result"
CONFIGURATION_SUBMIT = "configuration_submit"
EDIT_CHART = "edit_chart"
GET_CURRENT_USER = "get_current_user"
GOOGLE_DOC_WRITE = "google_doc_write"
GOOGLE_SHEET_WRITE = "google_sheet_write"
GOOGLE_SLIDES_WRITE = "google_slides_write"
IMAGE_GENERATE = "image_generate"
KB_ADD_FILE = "kb_add_file"
KB_GET_RECORD = "kb_get_record"
KB_LIST_TABLES = "kb_list_tables"
KB_OPERATION_STATUS = "kb_operation_status"
KB_QUERY_RECORDS = "kb_query_records"
KB_REMOVE_FILE = "kb_remove_file"
KB_SEARCH = "kb_search"
PRESENT_ARTIFACTS = "present_artifacts"
PYTHON_EXEC = "python_exec"
PYTHON_SANDBOX = "python_sandbox"
READ_CHART = "read_chart"
READ_REPORT = "read_report"
RECALL = "recall"
REMEMBER = "remember"
REMOVE_CHART = "remove_chart"
RENDER_DECK = "render_deck"
SEND_EMAIL = "send_email"
SKILL_INVOKE = "skill_invoke"
SKILL_LIST = "skill_list"
SKILL_READ = "skill_read"
SKILL_SAVE_GENERATED = "skill_save_generated"
SUBAGENT_RETURN = "subagent_return"
SUBAGENT_SPAWN = "subagent_spawn"
TEMPLATE_DOCUMENT_SUBMIT = "template_document_submit"
TOOL_LIST_WORKSPACES = "tool_list_workspaces"
TOOL_SWITCH_CONTEXT = "tool_switch_context"
WEB_CRAWL = "web_crawl"
WEB_DEEP_RESEARCH = "web_deep_research"
WEB_EXTRACT = "web_extract"
WEB_FETCH = "web_fetch"
WEB_MAP = "web_map"
WEB_SEARCH = "web_search"
WRITE_REPORT = "write_report"

# 现役工具名（**只含 canonical**）—— 契约测试 / role binding 校验用。
# ⚠ 改名前的旧名**不在这里**：它们不是工具，只是保留名，见 RESERVED_TOOL_NAMES。
# 混在一起会让「每个真工具都要显式选 kind」这类守卫对着一个不存在的名字要求元数据。
ALL_TOOL_NAMES: frozenset[str] = frozenset({
    "add_chart",
    "add_image",
    "agent_authoring_inspect",
    "agent_authoring_proposal_get",
    "agent_authoring_propose",
    "agent_authoring_tools",
    "agent_authoring_validate",
    "agent_change_get",
    "agent_change_propose",
    "agent_change_validate",
    "agent_ci_cancel",
    "agent_ci_get",
    "agent_ci_prepare",
    "agent_ci_request",
    "agent_ci_start",
    "agent_draft_start",
    "agent_eval_alignment_ask",
    "agent_eval_alignment_get",
    "agent_eval_alignment_report",
    "agent_eval_alignment_start",
    "agent_eval_case_proposal_get",
    "agent_eval_case_propose",
    "agent_eval_evidence_get",
    "agent_eval_suite_get",
    "agent_eval_suite_list",
    "agent_eval_turns_list",
    "agent_operation_get",
    "agent_operation_open",
    "agent_plugin_inspect",
    "agent_plugin_operation_get",
    "agent_plugin_save",
    "agent_plugin_validate",
    "agent_resource_get",
    "agent_resource_open",
    "agent_resource_search",
    "agent_skill_cancel",
    "agent_skill_inspect",
    "agent_skill_operation_get",
    "agent_skill_read",
    "agent_skill_save",
    "agent_skill_validate",
    "agent_tool_requirement_create",
    "agent_tool_requirement_get",
    "agent_tool_requirement_open",
    "agent_tool_requirement_reconcile",
    "agent_working_copy_open",
    "attachment_read",
    "bi_concept_resolve",
    "bi_query",
    "bi_schema",
    "canvas_add_external_iframe",
    "canvas_add_html_artifact",
    "canvas_add_insight",
    "canvas_add_spec_block",
    "canvas_create_design",
    "canvas_flatten_design",
    "canvas_update_design_text",
    "configuration_apply",
    "configuration_list",
    "configuration_open",
    "configuration_propose",
    "configuration_read",
    "configuration_result",
    "configuration_submit",
    "edit_chart",
    "get_current_user",
    "google_doc_write",
    "google_sheet_write",
    "google_slides_write",
    "image_generate",
    "kb_add_file",
    "kb_get_record",
    "kb_list_tables",
    "kb_operation_status",
    "kb_query_records",
    "kb_remove_file",
    "kb_search",
    "present_artifacts",
    "python_exec",
    "python_sandbox",
    "read_chart",
    "read_report",
    "recall",
    "remember",
    "remove_chart",
    "render_deck",
    "send_email",
    "skill_invoke",
    "skill_list",
    "skill_read",
    "skill_save_generated",
    "subagent_return",
    "subagent_spawn",
    "template_document_submit",
    "tool_list_workspaces",
    "tool_switch_context",
    "web_crawl",
    "web_deep_research",
    "web_extract",
    "web_fetch",
    "web_map",
    "web_search",
    "write_report",
})

# 保留名 = 现役名 ∪ 改名前的旧名。**user skill 不许撞的名字集合**
# (skill_upload.py G5)：旧名让出去会被用户 skill 占用，而历史 trace 里分不出来。
RESERVED_TOOL_NAMES: frozenset[str] = ALL_TOOL_NAMES | frozenset({
    "bi_ask_question",
    "bi_attribute",
    "bi_breakdown",
    "bi_call_llm_async",
    "bi_explain",
    "bi_fetch_account",
    "bi_fetch_rows",
    "bi_get_card",
    "bi_get_cards",
    "bi_get_llm_result",
    "bi_plan_query",
    "canvas_add_chart",
    "canvas_add_image",
    "canvas_delete_section",
    "canvas_patch_section",
    "canvas_reorder_sections",
})

# pseudo 条目（display-only，非注册 ToolSpec；如 python_sandbox 经
# skill_invoke 执行、仅 trace 显示用）——namespace 不相交测试的豁免集。
PSEUDO_TOOL_NAMES: frozenset[str] = frozenset({
    "python_sandbox",
})

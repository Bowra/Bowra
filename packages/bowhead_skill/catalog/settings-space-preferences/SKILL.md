---
name: settings-space-preferences
description: Explain or change current Space week start and stored-data language using authorized values and user confirmation. Exclude interface language, other Spaces and other Agents.
version: 1.0.5
title: Configure current Space preferences
category: platform
audience: peer
---

# Configure current Space preferences

Use the latest read receipt’s `schema` and `guidance` for field meanings, constraints and supported choices. `editable_fields`, `actions` and `save_policy` determine what this caller may do. Examples below describe the workflow; they do not override the registered read contract.

For supported preference work, read `configuration_read(form_id="workspace_i18n")`. The trusted context fixes the current Space; never invent or switch its ID. `week_start_day` affects weekly grouping. `default_data_language` describes stored data and affects translation for Space members; it does not change interface or answer language. Propose only fields admitted by the current read contract.

The read receipt's `target.workspace_name` is the authorized Space label. Use it when naming the Space; if absent, say "the current Space". Ignore other conversation, memory and Datarc workspace labels for this target. Mark `unset=true` or `value_source=default` as "default", even when asked for saved values; mark `unset=false` or `value_source=saved` as "saved". Example: "Week start: Monday (default). Data language: Chinese (default)." This read has no storage history; if asked about past choices, explain that it cannot establish them. Use concise user-facing labels; internal field names, provenance keys, Tool names and event statuses are for execution, not ordinary replies.

For an explicit interface-language request, briefly explain that this Agent cannot stage that field, report any actual original-page open request, then submit. Omit unrequested values and offers to change other settings. It controls product menus and labels; source-data language and AI-answer language cannot satisfy the request. Do not offer them as alternatives or reinterpret a clearly named setting. If the authorized original page is available, refer to its existing interface-language control without claiming manual-save permission or saving anything yourself.

If editable fields are empty, explain current settings and the permission boundary. For an explicit edit, call `configuration_open` to display the original page when available, then `configuration_propose` with only requested fields. Opening is not staging or saving. A successful proposed receipt persists the suggestion for review; `saved=false` means settings remain unchanged, not that the suggestion is unsaved. It waits for the user's explicit confirmation. Do not call apply to approve it.

A selected page fixes the send target and baseline even if the user switches Tabs. Newer manual input takes precedence: do not overwrite it or claim a conflicted suggestion was installed. Event delivery does not acknowledge browser rendering or confirmation readiness. Briefly say opening was requested or the suggestion is saved for review. Explain recovery only for an actual reported restriction, and invite review and confirmation without a technical event disclaimer. Use `configuration_result` for an owned proposal's actual status.

Deliver every outcome with `configuration_submit({"message":"Your answer grounded in the actual Tool receipts."})`. For a read-only request, submit the values and any actual open receipt; do not propose or apply. After staging a suggestion, submit immediately and explain that user confirmation is still pending. The user confirms after this execution ends. Do not wait, poll, restage the suggestion or end with ordinary assistant text. For a permission error or an ambiguous request, submit the restriction or clarification question through the same Tool. Pass only `message`; submitting it does not save settings or confirm a proposal.

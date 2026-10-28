---
name: settings-space-instructions
description: Explain or change the current Space's instructions, the text every conversation in the Space follows, through authorized reads and version-protected user confirmation. Exclude other Spaces, Space name or description, personal preferences and Agent prompts.
version: 1.0.1
title: Configure current Space instructions
category: platform
audience: peer
---

# Configure current Space instructions

Use the latest read receipt’s `schema` and `guidance` for field meanings, constraints and supported choices. `editable_fields`, `actions` and `save_policy` determine what this caller may do. Examples below describe the workflow; they do not override the registered read contract.

Space instructions are one free text per Space, written by its administrators, that every conversation in that Space follows: shared definitions, preferred sources, house style. Read `configuration_read(form_id="space_instructions")` and use only the server-bound current Space. They are not the Space name or description, not a personal preference and not another Agent's prompt.

Members may read them. Only Space administrators and Team managers change them, and only while the Space is active. If the read reports `space_archived`, say the Space is archived and must be restored before its instructions can change. Otherwise, if editable fields or write actions are unavailable, say who can change them and that the user can ask an administrator. Do not stage anything in either case.

For a change, call `configuration_propose` with `content` set to the **complete** new text within the returned length constraint. Start from the exact text the read returned and change only what the user asked: to add a rule, append it after a blank line; to edit or remove one, change only that line. Keep every other line word for word, in its order. Write the rule the way the user stated it, as a plain instruction; do not add headings, numbering or explanations they did not ask for. If the request conflicts with an existing line, ask which one should win instead of choosing. An empty `content` clears the instructions only when the user asked for that.

The server pins the current version. Wait for the user's confirmation on the suggestion card, which shows the line diff; do not call `configuration_apply`. Say the suggestion is saved for review and the instructions have not changed. A confirmed change becomes a new version that administrators can restore from the version history; there is no automatic undo. If the instructions changed after your read, report the conflict and read again instead of overwriting. Use `configuration_result` for the proposal's actual status; proposed never means saved.

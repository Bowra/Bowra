---
name: settings-space-metadata
description: Explain or change the current Space name and description through authorized metadata reads and revision-protected user confirmation. Exclude other Spaces, preferences, membership, credentials and Agent creation.
version: 1.0.3
title: Configure current Space metadata
category: platform
audience: peer
---

# Configure current Space metadata

Use the latest read receipt’s `schema` and `guidance` for field meanings, constraints and supported choices. `editable_fields`, `actions` and `save_policy` determine what this caller may do. Examples below describe the workflow; they do not override the registered read contract.

For supported metadata work, read `configuration_read(form_id="workspace_metadata")`. Use only the server-bound current Space. Use the returned name and description constraints; the name cannot be empty. An empty description clears it. These fields describe the Space, not another Agent, its prompt or its data language.

If editable fields or write actions are unavailable, explain the current authorization boundary. For example, “Rename this Space to Finance” changes only `name`; “Clear this Space's description” changes only `description` to an empty string. Clarify an ambiguous object instead of inventing an ID or replacing the Space from text.

Call `configuration_open` when available to display the original Space general page. The page also has independently saved preference controls; their presence does not admit them to this metadata task. For a clear interface-language request, explain that the Agent cannot stage that field. When the authorized original page is available, point to its existing interface-language control. Submit a brief unsupported-field explanation and any actual open request; omit unrequested values and offers to change other fields. Keep interface language distinct from source-data language and AI-answer language; do not offer either as an alternative or ask which one the user meant. Example: "I cannot suggest an interface-language change yet. You can review that control on the original settings page." For an admitted edit, call `configuration_propose` with only the requested fields. Opening and proposal review perform no settings write and do not change the manual candidate. The server pins the real metadata revision. Never generate a revision or user confirmation.

Wait for the user's confirmation through the existing surface. Event delivery does not acknowledge that the browser displayed the page or confirmation. Say the suggestion is saved for review, and ask the user to review when the original page is ready and local edits are settled. Do not claim the manual candidate was updated or the confirmation is visible. Do not call `configuration_apply` to approve metadata or offer automatic undo. Newer manual input and changed revisions take precedence; report a conflict and read again instead of overwriting. Use `configuration_result` for the original owned proposal's actual status. Finish from actual receipts; proposed or an open request never means saved.

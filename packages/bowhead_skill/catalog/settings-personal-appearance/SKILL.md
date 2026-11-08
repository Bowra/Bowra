---
name: settings-personal-appearance
description: Choose personal V3 appearance scalars from explicit user intent and current authorized Tool values. Use for dark mode, font profile, text scale, density, corners or motion; exclude colors, branding and other Agents.
version: 1.0.4
title: Choose personal appearance settings
category: platform
audience: peer
---

# Choose personal appearance settings

Use the latest read receipt’s `schema` and `guidance` for field meanings, constraints and supported choices. `editable_fields`, `actions` and `save_policy` determine what this caller may do. Examples below describe the workflow; they do not override the registered read contract.

Read `configuration_read(form_id="personal_appearance")` before preparing changes. Map the request to only the returned editable scalar fields and exact allowed values. Typography profile and text scale are distinct; clarify which one the user wants if ambiguous. Preserve unspecified values and existing V3 overrides. For an explicitly requested scalar reset, use the read's advertised `reset.fields` with `configuration_propose(reset=[...])`; this removes that personal override and inherits the Team/product value. Null is not a reset. Never propose historical `user_appearance`, a whole theme document, colors, chart styles, a whole-theme reset, schemaVersion or an etag.

Call `configuration_propose` with the sparse patch. When the read's `save_policy` is `explicit_appearance_request` and its `actions` include `apply`, the user explicitly asked for this change: call `configuration_apply` with the returned proposal, then say the change is saved and can be undone from its Chat card, or changed back on the appearance page when `undo_card_emitted` is false. Otherwise the proposal saves a suggestion and emits its confirmation event through the original personal appearance service; it does not save the setting or acknowledge browser rendering. Ask the user to review and confirm when the existing surface is ready, do not claim the confirmation is already displayed, and never call apply. If permissions or the baseline changed, read again and explain the conflict. Finish from actual Tool receipts.

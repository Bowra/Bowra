---
name: settings-language-preferences
description: Distinguish a persistent answer language preference from a one-turn instruction. Use for saved answer language, auto or follow_input; exclude interface language and source-data language.
version: 1.0.1
title: Change saved answer language
category: platform
audience: peer
---

# Change saved answer language

Use the latest read receipt’s `schema` and `guidance` for field meanings, constraints and supported choices. `editable_fields`, `actions` and `save_policy` determine what this caller may do. Examples below describe the workflow; they do not override the registered read contract.

Read `configuration_read(form_id="user_language")`. Only an explicit request to save a persistent preference and a server-grounded quote from the current original user message permit a patch. "This answer in English" changes only this answer: do not stage or apply a saved preference. Ask a concise question when persistence is ambiguous.

Use only returned choices. `auto` follows the question with the team default as fallback; `follow_input` follows the question without that fallback. Neither follows the interface language. UI language and stored-data language are separate settings.

For an admitted persistent request, call `configuration_propose`, then `configuration_apply` with the returned proposal ID. Proposal creation writes no setting and produces no extra confirmation card for this domain. Only an actual applied receipt means saved. On conflict, read the current value and do not invent success. Finish with `configuration_submit`.

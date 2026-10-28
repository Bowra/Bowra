---
name: settings-team-administration
description: Explain or prepare supported Team execution limits, model parameters, model assignments and retry settings using the current authorized configuration contract. Credential entry, connection creation/removal and model probes use the original dedicated workflows.
version: 1.0.0
title: Configure Team execution and models
category: platform
audience: peer
---

# Configure Team execution and models

Use `configuration_list` to discover the caller's available domains, then `configuration_read` for the requested form. Read its JSON Schema, guidance, current values, version, parameter profiles and editable fields. These receipts supply current fields and constraints; do not copy remembered limits or invent model/provider identifiers.

Prepare only the requested change. Preserve JSON types and all unrelated nested settings. A supplied object or array replaces that admitted field, so retain unrequested entries exactly as read. Explicit null follows the domain schema; it is not a reset instruction. Use `reset_all` only when the read advertises it and the user requested restoring that configuration unit. For execution limits, unlimited steps and automatic model-call duration are distinct from restoring defaults.

Call `configuration_propose`, then submit its actual receipt immediately with `configuration_submit`. Proposing saves a suggestion and changes no canonical setting. Team changes always require the user's review. Do not call `configuration_apply`, approve the suggestion, accept a risk, or wait/poll for confirmation in this execution.

Model configuration suggestions open in the original model settings editor. The user reviews the exact draft, readiness, any risk or migration acceptance and any embedding reindex before Apply. Retry changes use their own confirmation. Never claim verification, successful saving or new runtime behavior from a proposal. Model tests and credential entry are separate actions; do not request secret values in chat. If a connection must be created/removed or a secret/header/transport changed, explain that specific original-page step.

On a version conflict, read again before preparing a new proposal; preserve the user's requested intent without overwriting unrelated changes. If access or feature admission is unavailable, state that actual restriction. Only a successful result receipt establishes that a proposal was applied.

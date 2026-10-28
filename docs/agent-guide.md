# Build with Bowra: a guide for external agents

English | [简体中文](agent-guide.zh-CN.md)

Bowra is a self-hosted workspace for business analysis and reusable Agents. Its focus is **Verified Intelligence**: source references, numeric provenance, controlled calculations, and business definitions that a person can inspect alongside the result.

Use this guide to decide whether Bowra fits a task, identify required inputs, and discover the supported integration surface. Start with the [documentation index](README.md), [source setup](../deploy/README.md), or [examples](../examples/community/README.md).

## Capability map

“Community” below means included in the source preview. It does not imply that an operator has configured the required service or enabled an optional workflow.

| Capability                      | Inputs                                                                         | Outputs                                                                | Prerequisites                                                                                                                                                        | Status and entry                                                                                                                                                              |
| ------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Knowledge search                | Authorized documents and a question                                            | Answers with references to retrieved passages                          | Available indexed files, suitable model configuration, and file access                                                                                               | Community; [knowledge guide](help/knowledge-base.en-US.md)                                                                                                                    |
| Document matrix                 | Selected files and a reusable question set                                     | A file-by-question table with answers and source references            | Processed documents, supported extraction, and access to every selected file                                                                                         | Community; [supplier review example](../examples/community/supplier-review/README.md)                                                                                         |
| Business analysis               | A question, metric definitions, time range, filters, and authorized data       | Calculations, charts, tables, and an explanation to review             | Configured models and data tools; the current integration for governed business data is Datarc; sandbox-backed calculations require the compatible execution runtime | Community; [revenue review example](../examples/community/revenue-review/README.md)                                                                                           |
| Reports                         | Analysis results, evidence, intended audience, and requested scope             | A report in the workspace and supported downloadable exports           | Enabled report workflow; Canvas downloads require their rendering toolchain                                                                                          | Community; [report revision guide](help/revise-report.en-US.md); in-place revision depends on the deployment                                                                  |
| Native document Skills          | Structured content and an explicit requested file type                         | Basic DOCX, PDF, PPTX, or XLSX files                                   | The selected `bowra-*` Skill, file permissions, and local dependencies                                                                                               | Community; [document Skill guide](help/create-agent.en-US.md#choose-a-document-skill); template fidelity, OCR, and formula calculation are not supplied by these basic Skills |
| Specialist Agents and MCP tools | Instructions, selected capabilities, and authorized connections                | Reusable Agents and work using connected tools                         | Owner configuration, tool permissions, connection credentials, and required call approvals                                                                           | Community; [Agent creation](help/create-agent.en-US.md), [team MCP](help/team-mcp-tools.en-US.md), [personal MCP](help/personal-mcp.en-US.md)                                 |
| Agent version CI/CD             | A scoped API credential, an Agent-specific grant, and an identified Git change | A proposal, candidate, evaluation execution, and readiness information | Enabled automation/evaluation workflow and Developer API transport; owner acceptance is required                                                                     | Implemented, opt-in; [Developer API discovery](#discover-the-agent-developer-api) and [example](../examples/community/agent-automation/README.md)                             |

## What Verified Intelligence means for your output

Keep the evidence close to the claim. Document citations identify passages; sources from queries and code runs can expose result tables and execution output. Where recorded, numeric provenance connects figures to inputs and calculations. State the metric definition, units, time range, filters, and assumptions so a reviewer can check whether the calculation answers the intended question.

Report missing evidence or unavailable sources explicitly. A citation marker, a completed run, or an exported file does not establish that a conclusion is correct. Preserve uncertainty and distinguish supplied facts, computed results, and your interpretation. Never invent a query result or source reference to fill a gap.

## Integration boundaries

- **Connected MCP tools:** Bowra's Agents call tools exposed by configured external MCP services. Personal and Team connections have their own access and approval rules.
- **Agent Developer API:** external automation can participate in the scoped Agent version workflow described below. It does not grant general access to chats, knowledge, reports, or other users' resources.
- **General external task execution:** this source preview does not publish a supported MCP server or a general task-delegation API for another agent to invoke Bowra. Do not infer such an interface from internal browser routes or development utilities.

For an interactive analysis task, direct the authorized user to the configured Bowra workspace and the relevant guide. For repository changes, follow [AGENTS.md](../AGENTS.md) and [Contributing](../CONTRIBUTING.md).

## Discover the Agent Developer API

Use the public service address supplied by the deployment operator, including any path prefix such as `/bowhead`. There is no hosted Bowra endpoint assumed by this guide.

1. Open **Team settings → OpenAPI** with a Team owner or administrator. That page manages Community's Agent version credentials and exposes the deployment's documentation.
2. Read `<service-public-address>/api/openapi/v1/reference.md`. The generated reference describes the currently exposed endpoints and request fields. The owner/admin JSON documentation is at `/api/v1/team-openapi/documentation` under the same service address.
3. With an authorized key, call `GET /api/openapi/v1/scopes` using `Authorization: Bearer <token>`. It returns granted, installed scopes. A scope alone does not prove that version automation is enabled: check that the generated reference advertises the needed endpoints.
4. Use an Agent owner's automation grant that binds the permitted key, Agent, actions, repository/ref declaration, and expiry. The Agent workbench no longer offers a form to create grants; existing grants keep working. If the endpoints are absent, the operator must review availability first.

The Developer API transport is available in local deployments; SaaS transport requires a separate operator opt-in. Version automation and evaluation are also opt-in. Community inclusion does not certify a runnable CI/CD integration for an arbitrary deployment.

### Version workflow

All paths below are relative to `/api/openapi/v1`. Resolve exact bodies, identity fields, and current availability from the generated reference and responses.

| Step                                             | Endpoint                                                                | Required scope and grant action              |
| ------------------------------------------------ | ----------------------------------------------------------------------- | -------------------------------------------- |
| Read the current draft identity                  | `GET /agents/{agent_id}/version-context`                                | `agent_versions:read` + `gate:read`          |
| Submit a proposal for owner review               | `POST /agents/{agent_id}/version-proposals`                             | `agent_versions:write` + `proposal:create`   |
| Read proposal status and accepted draft identity | `GET /agents/{agent_id}/version-proposals/{proposal_id}`                | `agent_versions:read` + `gate:read`          |
| Prepare the owner-accepted draft                 | `POST /agents/{agent_id}/version-proposals/{proposal_id}/prepare`       | `agent_versions:write` + `candidate:prepare` |
| Run a candidate comparison                       | `POST /agents/{agent_id}/release-candidates/{candidate_id}/comparisons` | `agent_versions:write` + `eval:run`          |
| Read readiness and next-action identities        | `GET /agents/{agent_id}/release-candidates/{candidate_id}/gate`         | `agent_versions:read` + `gate:read`          |

![An external proposal proceeds through owner review, candidate evaluation, and a human release decision](assets/bowra/agent-lifecycle.svg)

Keep returned IDs, row versions, hashes, and idempotency keys as specified by the live contract. The repository/ref declaration records provenance and helps prevent mistakes; it is not independent proof of repository ownership. The actual authorization boundary is the active credential plus the Agent-specific, revocable grant and permitted actions.

Automation cannot accept its own proposal, acknowledge risks, or release a version on the owner's behalf. The owner decides what to accept and release, with administrator approval where required. To stop an external pipeline, disable or revoke its key in Team settings › OpenAPI. The [route implementation](../packages/bowhead_gateway/src/bowhead_gateway/routes/team_openapi.py) is available for code review; the running deployment's reference remains the starting point for integration.

## Plan a first adoption task

Choose one [example](../examples/community/README.md), use permitted inputs, and agree on the reviewable output before running it. Confirm source access, model/tool configuration, and any execution or export dependencies through [setup](../deploy/README.md). Then inspect the actual output and its evidence before widening the workflow.

Keep credentials in the operator's approved secret store. Never place API keys, private documents, or live customer data in a contribution or example. Community has no commercial activation or seat cap; model and infrastructure costs and configured usage budgets still apply.

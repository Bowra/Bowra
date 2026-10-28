# Bowra documentation

English | [简体中文](README.zh-CN.md)

**Verified Intelligence** means making business work inspectable: connect a claim to its source, a number to its calculation, and a result to the definitions used to produce it. Bowra brings these into a shared workspace for analysis, documents, reports, and reusable Agents.

[Build and setup](../deploy/README.md) · [For external agents](agent-guide.md) · [Examples](../examples/community/README.md) · [Project overview](../README.md)

## Choose your next step

| I want to…                       | Start here                                                                                                  | Then try                                                                     |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Run Bowra for the first time     | [Build from source](../deploy/README.md) and [create the first owner](../deploy/community-setup.md)         | [Start an analysis](help/getting-started.en-US.md)                           |
| Ask questions across documents   | [Knowledge base and document matrix](help/knowledge-base.en-US.md)                                          | [Supplier review example](../examples/community/supplier-review/README.md)   |
| Explain business results         | [Conversation workspace, reports, and sources](help/conversation-workbench.en-US.md)                        | [Revenue review example](../examples/community/revenue-review/README.md)     |
| Refine and share a report        | [Revise an existing report](help/revise-report.en-US.md)                                                    | Check source details and the downloaded result before sharing                |
| Build a specialist Agent         | [Create an Agent](help/create-agent.en-US.md) and [configure capabilities](help/team-capabilities.en-US.md) | Bind only the tools and Skills the task needs                                |
| Connect an MCP tool service      | [Team MCP tools](help/team-mcp-tools.en-US.md) or [personal MCP connections](help/personal-mcp.en-US.md)    | Check connection permissions and call approvals                              |
| Maintain Agent versions from CI  | The [Developer API guide](agent-guide.md#discover-the-agent-developer-api)                                  | [Agent automation example](../examples/community/agent-automation/README.md) |
| Contribute code or documentation | [Contributing](../CONTRIBUTING.md) and [repository agent instructions](../AGENTS.md)                        | [Security reporting](../SECURITY.md)                                         |

## Follow the evidence

![From authorized sources through analysis and evidence review to a shared result](assets/bowra/workflow.svg)

Verified Intelligence gives reviewers three things to inspect:

- **Sources:** citations can open document passages, query results, or code-run output. A missing or inaccessible source needs further investigation.
- **Numbers:** where a result has recorded numeric provenance or execution evidence, follow it back to the input and calculation. Check units, filters, dates, rounding, and missing data as well as the displayed total.
- **Definitions:** state what the metric means and which business rules apply. An accurately calculated number can still answer the wrong question if its scope is wrong.

Citations and successful execution support review; they do not guarantee that every generated conclusion is correct. The [conversation guide](help/conversation-workbench.en-US.md) explains source cards and report controls; the [knowledge guide](help/knowledge-base.en-US.md) explains source availability and document-matrix review.

## Before the first task

This repository is a **Community source preview**. Follow the setup guide for the supported prerequisites and commands. Gateway startup requires a compatible executor configuration; workflows that invoke sandbox execution additionally need its shared writable work directory. Model services and optional connections require the operator's own configuration and credentials.

Start with material you are allowed to use. Choose its Personal, Space, or Team visibility, then confirm it is available for the intended task. Sharing an Agent or a link does not grant access to its underlying documents or accounts. See [permissions](help/permissions.en-US.md).

Native Bowra document Skills and Canvas report exports use different toolchains. The setup guide explains their requirements; the [Agent creation guide](help/create-agent.en-US.md) describes the basic document Skills and their limits.

## Integration availability

The [external-agent guide](agent-guide.md) maps capabilities to inputs, outputs, prerequisites, and status. MCP lets Bowra's Agents use connected tools. A supported MCP server or general task-delegation API for external agents to invoke Bowra is not published in this source preview.

The scoped **Agent Developer API** supports the version proposal and evaluation workflow when enabled. Automation is opt-in; the Agent owner retains acceptance and release decisions. Check the running deployment's generated reference before integrating.

## Language and license

English is the default documentation entry; important guides have separate Chinese editions. Interface language follows an explicit saved choice, then a supported browser/system language, then English. See [language settings](help/language.en-US.md).

Community has no commercial activation key, renewal, or built-in user-count cap. Infrastructure, model costs, permissions, and configured budgets still apply. Source is covered by the [MIT License](../LICENSE), with [third-party notices](../THIRD_PARTY_NOTICES.md).

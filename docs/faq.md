# Bowra Community FAQ

English | [简体中文](faq.zh-CN.md)

## What is Bowra?

Bowra is an MIT-licensed, self-hosted AI agent platform for business data and document analysis. A shared workspace brings together specialist Agents, knowledge, calculations, and reports. Reviewers can follow available citations and calculation evidence back to the authorized inputs. See the [project overview](../README.md) and [documentation](README.md).

## What can I try with sample data?

- **Compare supplier proposals.** Ask the same questions across three synthetic proposals, compare prices and delivery terms in a document matrix, and open the cited passages. [Supplier review example](../examples/community/supplier-review/README.md)
- **Explain a revenue change.** Work from a synthetic CSV, check the totals by region and channel, and review a report against the stated metric definitions. [Revenue review example](../examples/community/revenue-review/README.md)
- **Review an Agent update.** Follow an enabled workflow from a proposed change through owner review, candidate evaluation, and release. [Agent automation example](../examples/community/agent-automation/README.md)

## How do I run Bowra Community?

Build it from the public source. You need Python 3.12, uv, Node.js 24, npm, and a compatible configured sandbox executor for Gateway startup. Set up the first owner and connect a model before sending a task. The operator provides the infrastructure, credentials, and any additional runtime required by the workflow. See the [source installation guide](../deploy/README.md) and [first-owner setup](../deploy/community-setup.md).

## What comes with Community?

Community includes the shared workspace, knowledge and document workflows, report editing, permissions, and configurable Agent workflows. The source is [MIT licensed](../LICENSE) and has no built-in user-count cap. Infrastructure, model usage, and configured budgets still apply. Bowhead Business provides separately distributed enterprise capabilities such as organization branding, enterprise identity, and organization-management APIs. See the [edition comparison](../README.md#community-and-business).

## Which tools and APIs can I connect?

Bowra Agents can use configured MCP tool services. Managed business data queries use an authorized Datarc connection. The scoped Agent Developer API supports the version proposal and evaluation workflow when enabled. The source preview ships an inbound MCP integration and an Agent lifecycle API with those specific roles. See the [integration and capability guide](agent-guide.md).

## How do I check a result?

Open the available source citations or recorded query and code outputs. Check the time range, units, filters, and metric definitions against the question. Evidence makes those checks possible, and the reviewer still decides whether the result supports the conclusion. See the [guide to reviewing evidence](README.md#follow-the-evidence).

## Where do I find the current version and security status?

The repository README links to the current source snapshot, its Community release notes, and upgrade instructions. The security policy lists relevant deployment boundaries and the procedure for reporting a vulnerability. [Current source snapshot](../README.md#current-source-snapshot) · [Security policy](../SECURITY.md)

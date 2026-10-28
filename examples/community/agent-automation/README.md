# Agent automation: proposals with an owner-controlled release

English | [简体中文](README.zh-CN.md) | [All examples](../README.md)

**Synthetic walkthrough:** a team maintains a revenue-review Agent. A repository change adds an instruction to calculate net revenue after refunds and explain missing causal evidence. CI can submit that proposal and evaluate an accepted candidate. The Agent owner chooses what to accept and release.

This is an integration walkthrough against the implemented API contract, not a runnable CI workflow or a claim that evaluations have passed. It requires a configured installation, real owner authorization and a working evaluation environment. No credentials or server addresses are provided.

## 1. Check availability and create a limited grant

Ask the deployment administrator whether Agent version automation is enabled. The implementation requires `FEATURE_AGENT_VERSION_AUTOMATION`, its owner-version dependency `FEATURE_AGENT_OWNER_VERSION_EVAL`, and `FEATURE_AGENT_EVAL_M3`. Eval and Owner version governance default on; automation defaults on wherever Team OpenAPI is served, including local mode. Explicit deployment disablement remains authoritative. The Team OpenAPI transport must also be served: local mode serves it; SaaS requires its separate `FEATURE_TEAM_OPENAPI_SAAS` opt-in. This example does not enable flags or supply a rollout plan.

Then use the supported UI:

1. A Team owner or administrator creates a key in **Team settings → OpenAPI**, limited to `agent_versions:write`. That scope includes the matching read scope. Store the token in your CI secret manager; the UI displays it only once.
2. The **Agent owner** opens that Agent's version workbench and creates/saves its working copy.
3. The key needs an existing Agent automation grant limited to the needed actions: `proposal:create`, `candidate:prepare`, `eval:run`, and `gate:read`, with a repository, ref and expiry. The Agent workbench no longer offers a form to create grants; existing grants keep working.

The repository and ref are caller-declared context, not cryptographic proof that a request came from GitHub. The actual authority is the Agent-specific, expiring, revocable grant bound to the key. Team administrator status alone does not replace Agent ownership.

## 2. Read the exact working copy

The API base is the installation's public address, including any configured prefix, followed by `/api/openapi/v1`. Authenticate with `Authorization: Bearer <your-token>`; never put the token in a URL or committed file.

Use `GET /scopes` to check the credential, then:

```text
GET /agents/{agent_id}/version-context
query: grant_id, repository, ref
```

Use the returned `working_copy_id`, `working_copy_row_version` and `base_release_id`. If `working_copy` is `null`, the owner must create/save a draft first. Do not invent an ID, guess a row version or use the live-release ID as a working-copy ID.

## 3. Submit a proposal; wait for owner acceptance

```text
POST /agents/{agent_id}/version-proposals
header: Idempotency-Key
body: grant_id, repository, ref, commit_sha,
      base_release_id, working_copy_id, working_copy_row_version,
      patch_hash, operations, atomic_groups, suggested_cases?, rationale?
```

This illustrative patch fragment replaces the whole system prompt of the synthetic example Agent. For a real Agent, review and preserve the existing instructions that remain necessary.

```json
{
  "operations": [
    {
      "op": "replace",
      "path": "/definition/system_prompt",
      "value": "Review the supplied synthetic revenue data. Calculate net revenue as gross revenue minus refunds. State the metric definitions, reconcile segment changes, and distinguish observations from causal hypotheses."
    }
  ],
  "atomic_groups": [{ "id": "revenue_rules", "operation_indexes": [0], "depends_on": [] }],
  "suggested_cases": [
    {
      "id": "refund_math",
      "task": "Gross revenue is 1000 USD and refunds are 100 USD. State net revenue and whether these numbers establish the cause of refunds.",
      "failure_mode": "Reports 1000 USD as net revenue or invents a causal explanation.",
      "covers_group_ids": ["revenue_rules"]
    }
  ]
}
```

`patch_hash` is `sha256:` followed by the SHA-256 hex digest of **only `operations`**, serialized as UTF-8 JSON with `ensure_ascii=False`, `sort_keys=True`, and separators `(',', ':')`. `commit_sha` records your actual repository revision. The fragment above is not a complete HTTP request; read version identities from the API and compute the hash from the exact submitted operations.

The returned proposal ID does not mean changes were accepted. Poll the following endpoint, using the same key and grant:

```text
GET /agents/{agent_id}/version-proposals/{proposal_id}
query: grant_id, repository, ref
```

The owner reviews/selects atomic groups in the workbench. Only after acceptance does `accepted_result` contain the exact working-copy ID and row version for the next step. Suggested evaluation cases still need the normal evaluation-plan review; submitting a suggestion does not prove it ran or passed.

## 4. Prepare the accepted candidate and evaluate it

| Action                            | Method and path                                                                               | Required grant action              | Required request context                                                                                                                                                     |
| --------------------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Prepare and run accepted proposal | `POST /agents/{agent_id}/version-proposals/{proposal_id}/run`                                 | `candidate:prepare` and `eval:run` | Common grant/repository/ref; optional `release_note`; `Idempotency-Key` header. The server reads the accepted working copy, Space and Policy                                 |
| Prepare accepted proposal         | `POST /agents/{agent_id}/version-proposals/{proposal_id}/prepare`                             | `candidate:prepare`                | Common grant/repository/ref; accepted working-copy ID and row version; `workspace_id`; optional `expected_eval_policy_revision_id`, `release_note`; `Idempotency-Key` header |
| Read candidate gate               | `GET /agents/{agent_id}/release-candidates/{candidate_id}/gate`                               | `gate:read`                        | Grant/repository/ref query parameters                                                                                                                                        |
| Start comparison                  | `POST /agents/{agent_id}/release-candidates/{candidate_id}/comparisons`                       | `eval:run`                         | Common context; `candidate_row_version`, `expected_control_row_version`, `candidate_hash`, `plan_id`, `plan_hash`; `Idempotency-Key` header                                  |
| Cancel this grant's comparison    | `POST /agents/{agent_id}/release-candidates/{candidate_id}/comparisons/{execution_id}/cancel` | `eval:run`                         | Common context; `expected_execution_row_version`                                                                                                                             |

`workspace_id` is the persisted API field for the authorized execution Space. Keep the technical name unchanged. Use identities returned by the preparation and gate responses; check readiness, `reason_codes` and `primary_action` rather than constructing candidate/plan hashes. Refresh and re-review when a version or policy conflict occurs. Reuse an idempotency key only for a retry of the same intended operation.

A `202` comparison response means the execution was accepted/queued, not that it passed. A model/runtime fault is not evidence of Agent quality. Record actual outcomes and direct the owner to failed or incomplete results.

## 5. The owner decides what goes live

The automation credential cannot accept a proposal, acknowledge risk on the owner's behalf, change audience or release a version. After inspecting the candidate and evaluation evidence, the owner uses the workbench's current action. Personal versions use the personal apply flow; wider visibility may require administrator approval. Do not invent a `/publish` or `/release` automation endpoint.

Useful completion evidence for your own integration is: the submitted proposal ID, its owner-accepted working-copy identity, the prepared candidate/plan identities, the actual comparison outcome, and the owner's subsequent release decision. This pack supplies none of those live outcomes.

Revoke the grant to stop its automation access. To stop an external pipeline, disable or revoke its key in Team settings › OpenAPI. Stop using the key when its grant expires or the Agent/owner is no longer eligible.

## Source of truth

- [Agent publication permissions](../../../docs/help/permissions/publish-agent.en-US.md)
- [Current API models and routes](../../../packages/bowhead_gateway/src/bowhead_gateway/routes/team_openapi.py)
- [Proposal operation validation](../../../packages/bowhead_core/src/bowhead_core/agent_change_proposal_store.py)

Read your installation's OpenAPI documentation before implementing a client; it omits Agent endpoints when automation is disabled. This guide describes the source contract and makes no CI, LLM or production deployment claim.

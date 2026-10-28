# Agent CI reference pipeline

This is an executable client for the existing Agent version workflow. It requires Python 3.12 on Linux/macOS and a deployment advertising `automation_protocol_version: 2`. See the [Agent Developer API guide](../../docs/agent-guide.md#discover-the-agent-developer-api) for the supported workflow and permissions.

An Owner opens the Agent draft and configures its evaluation Policy in the workbench. A Team administrator issues a Team key. The Owner's automation grant gives that principal `proposal:create`, `candidate:prepare`, `eval:run` and `gate:read` for the Agent, its evaluation Space and the intended repository/ref. The Agent workbench no longer offers a form to create grants; existing grants keep working. Give CI only that Team key. The client never uses an Owner cookie, accepts proposal groups, acknowledges risk, submits for review or activates a release.

Copy `proposal.json` to a reviewed `agent-proposal.json` in the CI repository. Change its allowed operations/groups and review any suggested tests. Store `BOWHEAD_CI_TOKEN` in the CI provider's secret store. Provision a private, durable checkpoint directory with mode `0700`; files are written with `0600`. It must survive process and runner interruption. A job-end artifact upload does not meet this requirement because a process can stop immediately after the server commits.

```sh
python3 scripts/agent-ci/run.py \
  --base-url https://bowhead.example.com \
  --agent-id osa_example --grant-id avag_example \
  --repository acme/agent --ref refs/heads/main \
  --commit-sha 0123456789abcdef0123456789abcdef01234567 \
  --payload agent-proposal.json \
  --checkpoint /private/agent-ci/osa_example-01234567.json \
  --timeout 900
```

Use the same arguments, payload and checkpoint to resume. A checkpoint contains operation keys, exact baseline/candidate/Plan/execution identities, context hashes and safe request preconditions. It contains no API token, prompt, patch content, test input or model response. A changed payload or principal cannot reuse it. Keep the original payload at the same reviewed revision. Protect checkpoints as internal metadata and retain them through the approval/recovery period. Different intended operations require different checkpoints; never delete a checkpoint just to clear a conflict.

The client checkpoints before each mutation and holds an exclusive file lock. On restart, proposal POST uses its original key; preparation and comparison first query their exact operation digests. The digest is `sha256(UTF8("automation:" + principal_id + ":" + original_client_key))`, prefixed once with `sha256:`. A terminal or changed-Plan operation is reported as such, never replaced with the latest run.

| Exit | Outcome                                                         | Meaning                                                                 |
| ---- | --------------------------------------------------------------- | ----------------------------------------------------------------------- |
| 0    | `ready_awaiting_human_release`                                  | This exact comparison is ready; a human still decides release.          |
| 0    | `released`                                                      | The exact candidate has an activation receipt.                          |
| 3    | `ready_with_risks_owner_acknowledgment_required`                | A human must review permitted soft risks.                               |
| 4    | Blocked, stale, authorization, configuration or intent conflict | Correct the stated issue; do not create replacement keys automatically. |
| 5    | Runtime/protocol failure                                        | Preserve the checkpoint, investigate and resume it.                     |
| 6    | Timeout                                                         | Waiting ended; preserve the checkpoint and resume later.                |

When the gate is blocked or unknown, the JSON outcome also carries the gate's `failure_summary`, and stderr gets one line such as `Required cases failing: 2 (introduced by this candidate: 1, also failing on Live: 0, not run on Live: 1). Review the cases at <owner link>.` When cases failed on expected points, the line adds `Unmet points: quality.1/p_3f2a, …` from `failure_summary.unmet_points` (up to 20). The summary holds counts and identifiers only; point text, case inputs and answers stay with the Owner. When the deployment has no public entry for the Team, the line names the Agent id instead of a link.

`--wait-release` keeps polling after a ready comparison until the exact candidate is activated or the timeout ends. Polling backs off to 15 seconds; each HTTP call has a 30-second timeout and at most four attempts. Retries preserve the original body and key. Stop/rejection is not treated as successful evaluation. To stop an external pipeline, disable or revoke its key in Team settings › OpenAPI.

`workflow.yml` is a manually invoked GitHub Actions example, not an enabled workflow in this repository. It requires a trusted self-hosted runner, protected `agent-evaluation` environment and pre-provisioned persistent volume (`CI_CHECKPOINT_ROOT`). Set the URL, Agent and grant as environment variables, and the token as an environment secret. Review the fixed commit before approving access to that environment; untrusted pull-request code must never receive the key. Repository/ref values are CI declarations, not verified workload identity. Other providers can run the same command against their protected persistent storage.

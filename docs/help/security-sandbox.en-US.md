# Security and offline sandbox

Code execution is available only with a separately configured, compatible sandbox and executor. The Community source preview alone does not include a validated runtime deployment. The boundaries below describe the supported isolation contract; deployment maintainers must verify them against the runtime actually installed.

Team owners and admins adjust the default and maximum duration in **Team settings › Agents › Run limits › Sandbox computation**. The page shows the deployment ceiling; the Team maximum cannot exceed it. Save changes for new tasks, or choose **Use deployment defaults**, review and save to inherit them again. Running tasks keep their initial limits.

Bowra runs untrusted, AI-generated code for a task in a constrained execution environment. The sandbox is a defense layer, not the only security control.

## What happens in an isolated computation

The task supplies explicit input files and a bounded command. The executor creates an isolated container, mounts only the required working data, applies runtime limits, collects allowed output, and destroys the environment after the task. The task does not receive the host filesystem, host credentials, or a general network connection.

## What “offline sandbox” means

The sandbox has no outbound network route. Code cannot fetch packages, call an external API, scan an internal network, or exfiltrate output over the network. Dependencies required for supported work are prebuilt into the image. If a task needs an external service, the product must handle that through an explicit, separately authorized integration rather than from sandbox code.

## Runtime boundaries

Supported Skill scripts run with Python 3.12, Node.js 24, or Bash 5; direct AI computations use Python.

An isolated computation requests 120 seconds by default; the deployment's default ceiling is 600 seconds. Administrators can tune these values. An execution timeout means the allotted time was exceeded; an unreachable executor means the service connection failed. If the response or cleanup is unconfirmed, the result is unknown: check the original run before retrying. A longer timeout cannot repair an unavailable service.

- **Identity and system calls:** code runs as a non-root user with a restricted syscall and capability profile.
- **Files:** only task-scoped input and output paths are available; host mounts are not writable.
- **Resources and lifetime:** CPU, memory, process, output, and wall-clock limits stop runaway work. The environment is removed after execution.
- **Executor:** the service that creates sandboxes validates the task contract and does not expose its own credentials to the workload.

## Common attack attempts

Network callbacks fail because there is no route. Reading host files, privileged operations, and mount changes fail because the container lacks host access and Linux privileges. Fork bombs, oversized output, and long-running loops are constrained by resource limits and timeout handling. These controls reduce blast radius; they do not make arbitrary code mathematically risk-free.

## Shared responsibilities

Users should provide the minimum necessary data and never place secrets in task material. Team administrators should restrict external integrations and data sources. Deployment maintainers should keep the sandbox runtime, executor image, host kernel, and container policy current; they should also verify network isolation, read-only mounts, non-root execution, capability dropping, and timeout cleanup after each relevant change.

No security system can promise it will never be bypassed. Bowra combines isolation, least privilege, bounded inputs, lifecycle cleanup, and operational patching so a defect in one layer is not automatically a host or data compromise.

Ask to reuse an earlier generated file when its contents do not need to change. The file appears in the current conversation’s Results under its original filename; an optional display title does not rename it. Reuse requires your current access to the original file in the same Space. It does not make the file public or skip document review. If storage cannot confirm the file, retry opening/presenting it after storage recovers; a missing preview does not require generating it again.

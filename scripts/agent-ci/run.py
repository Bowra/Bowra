#!/usr/bin/env python3
"""Provider-neutral Agent CI client with protected, durable mutation checkpoints."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4


class Outcome(Exception):
    def __init__(self, name: str, status: int, details: dict | None = None, message: str | None = None):
        self.name, self.status = name, status
        # Content-free extra fields printed with the outcome (for example the gate failure summary).
        self.details = details or {}
        # A human-readable line for the CI log, written to stderr.
        self.message = message


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def operation_hash(principal_id: str, key: str) -> str:
    return "sha256:" + hashlib.sha256(f"automation:{principal_id}:{key}".encode()).hexdigest()


class Checkpoint:
    """The caller holds the checkpoint lock throughout the command."""

    def __init__(self, path: Path, identity: dict):
        self.path = path
        if path.exists():
            if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise Outcome("checkpoint_permissions_invalid", 5)
            self.data = json.loads(path.read_text())
            if self.data.get("identity") != identity:
                raise Outcome("checkpoint_intent_conflict", 4)
        else:
            self.data = {"schema_version": 1, "identity": identity, "keys": {name: uuid4().hex for name in ("proposal", "prepare", "compare")}}
            self.save()

    def save(self) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".agent-ci-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical(self.data))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


class Client:
    def __init__(self, base_url: str, token: str):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise Outcome("base_url_invalid", 4)
        self.base_url, self.token = base_url.rstrip("/"), token
        # A redirect must never forward the Team credential to another endpoint.
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, path: str, *, query: dict | None = None, body: dict | None = None, key: str | None = None) -> dict:
        url = self.base_url + "/api/openapi/v1" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(url, data=None if body is None else canonical(body), headers=headers)  # noqa: S310 - validated HTTP(S) base URL
        for attempt in range(4):
            try:
                with self.opener.open(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    raise Outcome("authorization_required", 4) from None
                if exc.code in {409, 422, 423}:
                    raise Outcome("owner_action_or_intent_conflict", 4) from None
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise Outcome("protocol_failure", 5) from None
            except (urllib.error.URLError, TimeoutError):
                pass
            if attempt < 3:
                time.sleep(min(2**attempt, 8))
        raise Outcome("runtime_failure_resume_checkpoint", 5)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run(args, client: Client, checkpoint: Checkpoint, payload: dict) -> None:
    saved = checkpoint.data
    base = "/agents/" + urllib.parse.quote(args.agent_id, safe="")
    source = {"grant_id": args.grant_id, "repository": args.repository, "ref": args.ref}
    context = client.call(base + "/version-context", query=source)
    if context.get("automation_protocol_version") != 2:
        raise Outcome("protocol_upgrade_required", 4)
    principal = context["principal_id"]
    if saved.get("principal_id", principal) != principal:
        raise Outcome("checkpoint_principal_conflict", 4)
    saved["principal_id"] = principal
    keys = saved["keys"]
    if "proposal_id" not in saved:
        if "proposal_baseline" not in saved:
            if not context["working_copy"]:
                raise Outcome("owner_draft_required", 4)
            saved["proposal_baseline"] = context["working_copy"]
            checkpoint.save()
        body = {**source, **payload, **saved["proposal_baseline"], "commit_sha": args.commit_sha, "patch_hash": digest(payload["operations"])}
        result = client.call(base + "/version-proposals", body=body, key=keys["proposal"])
        saved["proposal_id"] = result["proposal_id"]
        checkpoint.save()
    proposal_path = base + "/version-proposals/" + urllib.parse.quote(saved["proposal_id"], safe="")
    prepare_query = {**source, "operation_key_hash": operation_hash(principal, keys["prepare"])}
    deadline = time.monotonic() + args.timeout
    delay = 1.0
    while time.monotonic() < deadline:
        state = client.call(proposal_path + "/automation-state", query=prepare_query)
        proposal = state["proposal"]
        if proposal["status"] not in {"open", "accepted"}:
            raise Outcome("proposal_not_accepted", 4)
        if proposal["status"] == "accepted" and state["preparation"] is None:
            if "prepare" not in saved:
                context = client.call(base + "/version-context", query=source)
                if context["preparation_blockers"] or not context["evaluation_context"]:
                    raise Outcome("owner_evaluation_configuration_required", 4)
                accepted = {
                    "working_copy_id": proposal["accepted_working_copy_id"],
                    "working_copy_row_version": proposal["accepted_working_copy_row_version"],
                }
                if any(context["working_copy"][key] != value for key, value in accepted.items()):
                    raise Outcome("accepted_draft_changed", 4)
                evaluation = context["evaluation_context"]
                saved["prepare"] = {
                    **source,
                    **accepted,
                    "workspace_id": evaluation["workspace_id"],
                    "expected_eval_policy_revision_id": evaluation["policy"]["revision_id"],
                    "expected_evaluation_context_hash": evaluation["context_hash"],
                }
                checkpoint.save()
            client.call(proposal_path + "/prepare", body=saved["prepare"], key=keys["prepare"])
            continue
        candidate = state["candidate"]
        if candidate:
            saved["candidate_id"] = candidate["candidate_id"]
            if candidate["state"] == "activated":
                saved["release_id"] = candidate["release_id"]
                checkpoint.save()
                raise Outcome("released", 0)
            if candidate["state"] in {"withdrawn", "rejected", "stale", "superseded"}:
                raise Outcome("candidate_terminal_without_release", 4)
            candidate_path = base + "/release-candidates/" + urllib.parse.quote(candidate["candidate_id"], safe="")
            recovered = client.call(
                candidate_path + "/comparison-operation", query={**source, "operation_key_hash": operation_hash(principal, keys["compare"])}
            )
            comparison = recovered["execution"]
            if comparison is not None and not (recovered.get("client_intent_verified") and recovered.get("repository_context_verified")):
                raise Outcome("legacy_execution_requires_review", 4)
            if comparison is None:
                if "compare" not in saved:
                    saved["compare"] = {
                        **source,
                        **{key: candidate[key] for key in ("candidate_row_version", "candidate_hash", "plan_id", "plan_hash")},
                        "expected_control_row_version": candidate["control_row_version"],
                    }
                    checkpoint.save()
                client.call(candidate_path + "/comparisons", body=saved["compare"], key=keys["compare"])
                continue
            saved["execution_id"] = comparison["id"]
            checkpoint.save()
            if comparison["status"] in {"failed", "cancelled"}:
                if comparison.get("failure_code") in {"agent_eval_candidate_stale", "agent_eval_plan_stale", "agent_eval_workspace_scope_changed"}:
                    raise Outcome("evaluation_evidence_stale", 4)
                raise Outcome("evaluation_runtime_failure", 5)
            if comparison["status"] == "completed":
                if (
                    not candidate["gate_applicable"]
                    or candidate["snapshot_execution_id"] != comparison["id"]
                    or candidate["plan_id"] != comparison["plan_id"]
                ):
                    raise Outcome("evaluation_evidence_stale", 4)
                readiness = candidate["readiness"]
                if readiness == "ready_with_risks":
                    raise Outcome("ready_with_risks_owner_acknowledgment_required", 3)
                if readiness != "ready":
                    summary = gate_failure_summary(client, candidate_path, source)
                    raise Outcome(
                        "evaluation_blocked_or_unknown",
                        4,
                        {"failure_summary": summary} if summary else None,
                        failure_summary_line(summary, args.agent_id) if summary else None,
                    )
                if not args.wait_release:
                    raise Outcome("ready_awaiting_human_release", 0)
        time.sleep(min(delay, max(0, deadline - time.monotonic())))
        delay = min(delay * 1.5, 15)
    raise Outcome("timeout_resume_checkpoint", 6)


def gate_failure_summary(client: Client, candidate_path: str, source: dict) -> dict | None:
    """The gate's content-free failure counts and Owner link; an unreadable gate yields None."""
    try:
        summary = client.call(candidate_path + "/gate", query=source).get("failure_summary")
    except Outcome:
        return None
    return summary if isinstance(summary, dict) else None


def failure_summary_line(summary: dict, agent_id: str) -> str:
    where = summary.get("owner_url") or f"the Agent {agent_id} Release tab"
    return (
        f"Required cases failing: {summary.get('required_failed', 0)} "
        f"(introduced by this candidate: {summary.get('introduced', 0)}, "
        f"also failing on Live: {summary.get('also_failing_on_live', 0)}, "
        f"not run on Live: {summary.get('not_run_on_live', 0)}).{_unmet_points_clause(summary)} Review the cases at {where}."
    )


def _unmet_points_clause(summary: dict) -> str:
    """Case and point ids only; the gate never sends point text or answers."""
    points = [item for item in summary.get("unmet_points") or [] if isinstance(item, dict)]
    if not points:
        return ""
    return " Unmet points: " + ", ".join(f"{item.get('case_key')}/{item.get('point_id')}" for item in points) + "."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("base-url", "agent-id", "grant-id", "repository", "ref", "commit-sha", "payload", "checkpoint"):
        parser.add_argument("--" + field, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--wait-release", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("BOWHEAD_CI_TOKEN")
    if not token or not 1 <= args.timeout <= 86400:
        raise Outcome("credentials_or_timeout_invalid", 4)
    payload = json.loads(Path(args.payload).read_text())
    if set(payload) - {"operations", "atomic_groups", "suggested_cases", "rationale"} or not payload.get("operations"):
        raise Outcome("payload_invalid", 4)
    path = Path(args.checkpoint).absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise Outcome("checkpoint_directory_permissions_invalid", 4)
    identity = {key: getattr(args, key) for key in ("base_url", "agent_id", "grant_id", "repository", "ref", "commit_sha")}
    identity["payload_hash"] = digest(payload)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args, Client(args.base_url, token), Checkpoint(path, identity), payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Outcome as outcome:
        sys.stdout.write(json.dumps({"outcome": outcome.name, **outcome.details}) + "\n")
        if outcome.message:
            sys.stderr.write(outcome.message + "\n")
        sys.exit(outcome.status)
    except (OSError, ValueError, KeyError, TypeError):
        sys.stdout.write(json.dumps({"outcome": "invalid_input_or_checkpoint"}) + "\n")
        sys.exit(4)

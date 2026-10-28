"""Power BI Service Principal discovery and query permission verification."""

from __future__ import annotations

from typing import Any

import httpx2
from bowhead_core.pbi_client import PBI_API, PbiClient, PbiClientError, PbiTokenProvider
from bowhead_core.published_diagnostics import publish_powerbi_admin_failure
from fastapi import HTTPException


def _coded_failure(status_code: int, code: str) -> HTTPException:
    detail: dict[str, Any] = {"code": code}
    published = publish_powerbi_admin_failure(code)
    if published is not None:
        detail["diagnostics"] = published.to_wire()
    return HTTPException(status_code, detail=detail)


async def _access_token(config: dict[str, str]) -> str:
    provider = PbiTokenProvider(config)
    try:
        async with httpx2.AsyncClient(timeout=15.0) as http:
            return await provider.get_token(http=http)
    except PbiClientError as exc:
        if exc.code == "pbi_token_refresh_rejected":
            raise _coded_failure(401, "pbi_connection_auth_rejected") from exc
        raise _coded_failure(503, "pbi_connection_discovery_unreachable") from exc


async def _collection(http: httpx2.AsyncClient, url: str, token: str) -> list[dict[str, Any]]:
    try:
        response = await http.get(url, headers={"Authorization": f"Bearer {token}"})
    except (httpx2.TimeoutException, httpx2.RequestError) as exc:
        raise _coded_failure(503, "pbi_connection_discovery_unreachable") from exc
    if response.status_code == 401:
        raise _coded_failure(401, "pbi_connection_auth_rejected")
    if response.status_code == 403:
        raise _coded_failure(403, "pbi_connection_access_denied")
    if response.status_code == 404:
        raise _coded_failure(404, "pbi_connection_not_found")
    if response.status_code >= 400:
        raise _coded_failure(502, "pbi_connection_discovery_failed")
    try:
        body = response.json()
        value = body.get("value") if isinstance(body, dict) else None
    except ValueError as exc:
        raise _coded_failure(502, "pbi_connection_discovery_failed") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _coded_failure(502, "pbi_connection_discovery_failed")
    return value


async def _discover(config: dict[str, str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    token = await _access_token(config)
    async with httpx2.AsyncClient(timeout=20.0) as http:
        groups = await _collection(http, f"{PBI_API}/groups", token)
        workspaces = [
            {"id": str(group.get("id") or ""), "name": str(group.get("name") or "")}
            for group in groups
            if isinstance(group.get("id"), str) and group.get("id")
        ]
        workspace_id = config.get("workspace_id") or (workspaces[0]["id"] if workspaces else "")
        if not workspace_id:
            return workspaces, []
        datasets = await _collection(http, f"{PBI_API}/groups/{workspace_id}/datasets", token)
        return workspaces, [
            {"id": str(dataset.get("id") or ""), "name": str(dataset.get("name") or "")}
            for dataset in datasets
            if isinstance(dataset.get("id"), str) and dataset.get("id")
        ]


async def _build_datasets(config: dict[str, str], datasets: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return only datasets for which the SPN can execute DAX (Power BI's Build check)."""
    if not config.get("workspace_id"):
        return []
    provider = PbiTokenProvider(config)
    allowed: list[dict[str, str]] = []
    for dataset in datasets:
        result = await PbiClient(
            workspace_id=config["workspace_id"],
            dataset_id=dataset["id"],
            token_provider=provider,
        ).execute_dax('EVALUATE ROW("bowhead_build_probe", 1)')
        if result.error_code is None:
            allowed.append(dataset)
    return allowed


async def _resolved_config(config: dict[str, str]) -> dict[str, str]:
    workspaces, datasets = await _discover(config)
    if not config["workspace_id"]:
        if config["dataset_id"]:
            if not workspaces:
                raise _coded_failure(404, "pbi_connection_not_found")
            config["workspace_id"] = workspaces[0]["id"]
            return config
        # Choose the first workspace containing a dataset the SPN can execute. A workspace
        # can be visible while all its datasets lack Build, so do not stop at the first group.
        for workspace in workspaces:
            candidate = {**config, "workspace_id": workspace["id"]}
            _, candidate_datasets = await _discover(candidate)
            provider = PbiTokenProvider(candidate)
            for dataset in candidate_datasets:
                result = await PbiClient(
                    workspace_id=candidate["workspace_id"],
                    dataset_id=dataset["id"],
                    token_provider=provider,
                ).execute_dax('EVALUATE ROW("bowhead_connection_probe", 1)')
                if result.error_code is None:
                    config["workspace_id"] = workspace["id"]
                    config["dataset_id"] = dataset["id"]
                    return config
        raise _coded_failure(403, "pbi_connection_auth_rejected")
    if config["dataset_id"]:
        return config
    if not datasets:
        raise _coded_failure(404, "pbi_connection_not_found")

    # The REST dataset listing does not return a Build-permission flag. executeQueries is
    # Power BI's authoritative permission check, so select the first dataset that accepts a
    # harmless DAX probe. Keep this server-side and never disclose provider response text.
    provider = PbiTokenProvider(config)
    for dataset in datasets:
        result = await PbiClient(
            workspace_id=config["workspace_id"],
            dataset_id=dataset["id"],
            token_provider=provider,
        ).execute_dax('EVALUATE ROW("bowhead_connection_probe", 1)')
        if result.error_code is None:
            config["dataset_id"] = dataset["id"]
            return config
    raise _coded_failure(403, "pbi_connection_access_denied")

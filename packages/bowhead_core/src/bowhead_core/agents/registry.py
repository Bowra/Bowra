"""Agent registry — system + user 双源 manifest loader (PR-#1 system / PR-#2 user).

System: 启动 load 一次, 不热更.
User: per-workspace, 启动 load 一次到 in-memory cache;
      admin upload endpoint 触发 invalidate + reload single workspace (PR-#2 留 hook).

Path traversal/symlink 防御 (T-SEC-P0.1):
- 上传 + 启动加载都过 `_validate_path()`
- reject path 含任何 symlink (含中间 parent)
- realpath resolve 后 startswith base 校验

JSONSchema 校验 (input_schema / output_schema):
- 必须是 valid JSONSchema
- 根 `additionalProperties` 必须 `False` (防 prompt-injection 注入额外字段)

强约束 (PR-#2 enforce, 见 manifest_security.py):
- user 不可设 `trust_class`, `output_schema`, `tools.deny`
- user `model` 不可超过 plan tier
- user `system.append` 反 prompt-injection lint
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ManifestSecurityError, ManifestValidationError
from .manifest import AgentManifest
from .manifest_security import enforce_user_constraints, merge_user_over_system
from .metrics import emit_manifest_load_fail

logger = logging.getLogger(__name__)


def _active_capability_ids() -> frozenset[str]:
    """Code-owned active Capability IDs used by manifest load-time guards."""

    from bowhead_core.agents.capability_roles import active_capability_ids

    return active_capability_ids()


# System manifest base directory, relative to the bowhead_core package root
_PACKAGE_ROOT = Path(__file__).parent
_SYSTEM_MANIFEST_DIR = _PACKAGE_ROOT / "manifests" / "system"
_SYSTEM_PROMPT_DIR = _PACKAGE_ROOT / "prompts" / "system"
_USER_MANIFEST_DIR = _PACKAGE_ROOT / "manifests" / "user"
_USER_PROMPT_DIR = _PACKAGE_ROOT / "prompts" / "user"


def _validate_path(path: Path, base: Path) -> Path:
    """Path traversal + symlink 防御 (T-SEC-P0.1).

    Reject:
    - path 是 symlink
    - 中间 parent 含 symlink
    - realpath 不在 base 下

    Returns realpath if OK, raises ManifestSecurityError otherwise.
    """
    if not path.exists():
        raise ManifestValidationError(f"path does not exist: {path}")

    # T-SEC-P0.1: reject symlinks (including any symlink in intermediate path components)
    if path.is_symlink():
        raise ManifestSecurityError(f"symlink not allowed: {path}")
    base_real = base.resolve()
    cur = path
    while cur not in (base_real, cur.parent):
        if cur.is_symlink():
            raise ManifestSecurityError(f"parent path contains symlink: {cur}")
        cur = cur.parent
        if not cur.is_relative_to(base_real) and cur != base_real:
            break

    # Resolved realpath starts with base
    real = path.resolve(strict=True)
    if not real.is_relative_to(base_real):
        raise ManifestSecurityError(f"path traversal: {path} resolves to {real}, outside base {base_real}")
    return real


def _validate_jsonschema(schema: dict[str, Any], field_name: str) -> None:
    """JSONSchema 校验 + 根 `additionalProperties=False` 强制."""
    if "type" not in schema:
        raise ManifestValidationError(f"{field_name}: JSONSchema must have 'type'")
    if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
        raise ManifestValidationError(f"{field_name}: object schema must have additionalProperties=false (防 prompt injection)")


class AgentRegistry:
    """全局 agent manifest 注册中心 (system + user 双源).

    使用方式:
        registry = AgentRegistry.bootstrap()                      # 仅 system
        registry.load_user_workspace("ws_1", plan_tier="enterprise")  # PR-#2 加 user
        m = registry.get("compute", workspace_id="ws_1")          # user override system
    """

    def __init__(self) -> None:
        self._system: dict[str, AgentManifest] = {}
        # workspace_id → name → manifest
        self._user: dict[str, dict[str, AgentManifest]] = {}

    @classmethod
    def bootstrap(
        cls,
        system_dir: Path | None = None,
    ) -> AgentRegistry:
        """启动 load 所有 system manifest.

        Args:
            system_dir: override default system manifest dir (test 用).
        """
        registry = cls()
        registry._load_system(system_dir or _SYSTEM_MANIFEST_DIR)
        return registry

    # -- System manifest --

    def _load_system(self, system_dir: Path) -> None:
        if not system_dir.is_dir():
            logger.warning("system manifest dir not found, skipping: %s", system_dir)
            return
        loaded = 0
        for yaml_path in sorted(system_dir.glob("*.yaml")):
            try:
                manifest = self._load_one(
                    yaml_path,
                    base_dir=system_dir,
                    is_system=True,
                    plan_tier=None,
                )
            except (ManifestValidationError, ManifestSecurityError) as e:
                # Fail fast at startup (one invalid manifest prevents startup)
                emit_manifest_load_fail(
                    source="system",
                    reason=type(e).__name__,
                )
                raise ManifestValidationError(f"failed to load system manifest {yaml_path}: {e}") from e
            if manifest.name in self._system:
                raise ManifestValidationError(f"{yaml_path}: duplicate system manifest name '{manifest.name}'")
            self._system[manifest.name] = manifest
            loaded += 1
        logger.info("loaded %d system manifests from %s", loaded, system_dir)

    # -- User manifest (PR-#2) --

    def load_user_workspace(
        self,
        workspace_id: str,
        *,
        plan_tier: str,
        user_dir: Path | None = None,
        prompt_dir: Path | None = None,
    ) -> None:
        """Load 1 个 workspace 的 user-defined manifests.

        Plan-tier 校验在 enforce_user_constraints 内 (per-manifest).
        本方法 plan_tier 用于 enforce 校验 + 整体 dispatch endpoint 鉴权 (admin
        上传 endpoint 必须 enterprise tier).

        Args:
            workspace_id: 已 auth 过的 workspace id (调用方负责).
            plan_tier: workspace 的 plan tier ("free" / "pro" / "enterprise").
            user_dir: override base for user manifest (test 用).
            prompt_dir: override base for user prompt (test 用).

        Raises:
            ManifestValidationError / ManifestSecurityError on per-manifest fail.
        """
        ws_manifest_dir = (user_dir or _USER_MANIFEST_DIR) / workspace_id
        if not ws_manifest_dir.is_dir():
            # No user manifest for a workspace is not an error; use an empty dict.
            self._user[workspace_id] = {}
            logger.info(
                "no user manifests for workspace %s (dir not found)",
                workspace_id,
            )
            return
        ws_user: dict[str, AgentManifest] = {}
        for yaml_path in sorted(ws_manifest_dir.glob("*.yaml")):
            try:
                manifest = self._load_one(
                    yaml_path,
                    base_dir=ws_manifest_dir,
                    is_system=False,
                    plan_tier=plan_tier,
                    user_prompt_dir=(prompt_dir or _USER_PROMPT_DIR) / workspace_id,
                )
            except (ManifestValidationError, ManifestSecurityError) as e:
                emit_manifest_load_fail(source="user", reason=type(e).__name__)
                raise
            ws_user[manifest.name] = manifest
        self._user[workspace_id] = ws_user
        logger.info("loaded %d user manifests for workspace %s", len(ws_user), workspace_id)

    # -- Common loader --

    def _load_one(
        self,
        yaml_path: Path,
        *,
        base_dir: Path,
        is_system: bool,
        plan_tier: str | None = None,
        user_prompt_dir: Path | None = None,
    ) -> AgentManifest:
        """Load + validate 单 yaml manifest, 返回 AgentManifest.

        Args:
            yaml_path: yaml 文件 path.
            base_dir: yaml base dir (path traversal anchor).
            is_system: True → 系统 manifest, False → user.
            plan_tier: user manifest 必填 (用于强约束校验).
            user_prompt_dir: user manifest 必填 (prompt md base dir).
        """
        _validate_path(yaml_path, base_dir)

        with yaml_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ManifestValidationError(f"{yaml_path}: yaml root must be a mapping")

        # User manifest: strict constraints (run before Pydantic validation)
        if not is_system:
            if plan_tier is None:
                raise ManifestValidationError(f"{yaml_path}: plan_tier required for user manifest")
            try:
                enforce_user_constraints(data, plan_tier=plan_tier)
            except (ManifestValidationError, ManifestSecurityError) as e:
                raise type(e)(f"{yaml_path}: {e}") from e
            # User cannot set trust_class → inject default "read-mostly" to pass Pydantic validation
            data.setdefault("trust_class", "read-mostly")

        # Pydantic schema validate
        try:
            manifest = AgentManifest.model_validate(data)
        except Exception as e:
            raise ManifestValidationError(f"{yaml_path}: {e}") from e
        if manifest.primary_capability is not None and manifest.primary_capability not in _active_capability_ids():
            raise ManifestValidationError(
                f"{yaml_path}: primary_capability must reference an active Capability Definition: {manifest.primary_capability}",
            )

        # Filename must match the name field.
        if yaml_path.stem != manifest.name:
            raise ManifestValidationError(f"{yaml_path}: filename stem '{yaml_path.stem}' != manifest.name '{manifest.name}'")

        # JSONSchema validation
        if manifest.input_schema:
            _validate_jsonschema(manifest.input_schema, "input_schema")
        if manifest.output_schema:
            _validate_jsonschema(manifest.output_schema, "output_schema")

        # System prompt file must exist and be within the allowed base directory.
        # Sprint θ Phase 8b-4: Lead manifest has system: ~ (None) — bypass file
        # validation. Lead orchestrator builds prompt in-memory via prompts/lead.py
        # per F2 fix.
        if manifest.system is not None:
            prompt_base = _SYSTEM_PROMPT_DIR if is_system else user_prompt_dir
            if prompt_base is None:
                raise ManifestValidationError(f"{yaml_path}: prompt_dir not provided for user manifest")
            prompt_path = (prompt_base / manifest.system.file).resolve()
            try:
                _validate_path(prompt_path, prompt_base)
            except (ManifestValidationError, ManifestSecurityError) as e:
                raise ManifestValidationError(f"{yaml_path}: system.file invalid — {e}") from e

        # tools.allow / deny must not intersect.
        allow_set = set(manifest.tools.allow)
        deny_set = set(manifest.tools.deny)
        if allow_set & deny_set:
            raise ManifestValidationError(f"{yaml_path}: tools.allow ∩ tools.deny != ∅: {allow_set & deny_set}")

        return manifest

    # -- Public API --

    def validate_callable_peers(self, workspace_id: str | None = None) -> None:
        """v2: 启动后调一次 — 验证所有 manifest 的 callable_peers 都引用真存在的 agent.

        load 时不能检查 (agents 加载顺序未定), 此方法在全部 load 后调.
        Raises ManifestValidationError if 任何 peer 不存在.
        """
        all_visible = set(self.list_all(workspace_id=workspace_id))
        for name in self.list_all(workspace_id=workspace_id):
            manifest = self.get(name, workspace_id=workspace_id)
            for peer in manifest.callable_peers:
                if peer not in all_visible:
                    raise ManifestValidationError(f"agent {name!r} callable_peers references non-existent agent {peer!r}")

    def get(
        self,
        name: str,
        workspace_id: str | None = None,
    ) -> AgentManifest:
        """拿 manifest by name. user override system.

        Args:
            name: agent name.
            workspace_id: 若指定且该 workspace 有同名 user manifest, 则返 user
                version (override system); 否则 fallback to system.

        Raises:
            KeyError: name 既不在 system 也不在 (workspace_id 的 user) 里.
        """
        if workspace_id is not None and workspace_id in self._user:
            ws_user = self._user[workspace_id]
            if name in ws_user:
                if name in self._system:
                    return merge_user_over_system(ws_user[name], self._system[name])
                return ws_user[name]
        if name in self._system:
            return self._system[name]
        raise KeyError(f"unknown agent: {name} (workspace_id={workspace_id})")

    def list_all(self, workspace_id: str | None = None) -> list[str]:
        """所有可见 manifest 名 (system + workspace 的 user)."""
        names: set[str] = set(self._system.keys())
        if workspace_id is not None and workspace_id in self._user:
            names.update(self._user[workspace_id].keys())
        return sorted(names)

    def __len__(self) -> int:
        return len(self._system)

    def __contains__(self, name: str) -> bool:
        return name in self._system

    def has_user_manifest(self, workspace_id: str, name: str) -> bool:
        return workspace_id in self._user and name in self._user[workspace_id]

    def invalidate_workspace(self, workspace_id: str) -> None:
        """Admin upload endpoint 调此 + load_user_workspace 重 load (PR-#2 hook)."""
        self._user.pop(workspace_id, None)

"""skill-import 线的 feature flag（批 2 起）."""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def bundle_import_draft_enabled() -> bool:
    """批 2 `<bundle_import>`：把包清点结果说给 Lead，并让它问用户要处理哪些组件（默认关）。

    关时行为与批 1 完全一致：清点照跑、能力组件照样不进 KB、skill 照样落成待处理导入，
    只是 Lead 不知道这些，也不会主动问。
    """
    return os.environ.get("FEATURE_BUNDLE_IMPORT_DRAFT", "0").strip().lower() in _TRUTHY

"""Sprint ψ feature flags — runtime-toggleable via env.

Reads env on every access (cheap dict lookup) so /admin/flags POST that
mutates os.environ takes effect without process restart. This is the
correct pattern per Sprint ψ tech-design r2 §0.1 (post-CTO/SRE/Eng review).

Defaults: all ON. Set `BOWHEAD_PSI_<NAME>=0` to disable.
"""

from __future__ import annotations

import os

_FLAG_DEFAULTS = {
    "PSI_CAP_ENABLED": "1",
    "PSI_EAGER_COMPOSE": "1",
    "PSI_PIN_INJECT": "1",
}


def get(name: str) -> bool:
    """Read flag from env each call. Re-evaluates on every check so
    /admin/flags mutation of os.environ takes effect without restart."""
    default = _FLAG_DEFAULTS.get(name, "1")
    return os.environ.get(f"BOWHEAD_{name}", default) == "1"


def all_flags_dict() -> dict[str, bool]:
    """For /health endpoint exposure."""
    return {k.lower(): get(k) for k in _FLAG_DEFAULTS}

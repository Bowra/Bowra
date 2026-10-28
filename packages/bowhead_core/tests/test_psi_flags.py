"""UT for ψ-0 psi_flags accessor."""

import os
from unittest.mock import patch

import pytest

from bowhead_core import psi_flags


@pytest.mark.unit
def test_defaults_all_on():
    """Without env override, the surviving flags default ON."""
    # Clean any env to be sure
    with patch.dict(os.environ, {}, clear=False):
        for name in [
            "BOWHEAD_PSI_CAP_ENABLED",
            "BOWHEAD_PSI_EAGER_COMPOSE",
            "BOWHEAD_PSI_PIN_INJECT",
        ]:
            os.environ.pop(name, None)
        assert psi_flags.get("PSI_CAP_ENABLED") is True
        assert psi_flags.get("PSI_EAGER_COMPOSE") is True
        assert psi_flags.get("PSI_PIN_INJECT") is True


@pytest.mark.unit
def test_env_override_off():
    """BOWHEAD_PSI_X=0 returns False."""
    with patch.dict(os.environ, {"BOWHEAD_PSI_CAP_ENABLED": "0"}):
        assert psi_flags.get("PSI_CAP_ENABLED") is False


@pytest.mark.unit
def test_env_runtime_change_takes_effect():
    """Mutating os.environ mid-process WITHOUT module reload changes return.
    This is the core P0-3 fix: flag value is per-call, not per-import.
    """
    with patch.dict(os.environ, {"BOWHEAD_PSI_CAP_ENABLED": "1"}):
        assert psi_flags.get("PSI_CAP_ENABLED") is True
    with patch.dict(os.environ, {"BOWHEAD_PSI_CAP_ENABLED": "0"}):
        assert psi_flags.get("PSI_CAP_ENABLED") is False
    # And back to default after exit:
    os.environ.pop("BOWHEAD_PSI_CAP_ENABLED", None)
    assert psi_flags.get("PSI_CAP_ENABLED") is True


@pytest.mark.unit
def test_unknown_flag_defaults_true():
    """Unknown flag name defaults to 1 (ON) — keeps safe-open semantics."""
    assert psi_flags.get("NONEXISTENT_FLAG") is True


@pytest.mark.unit
def test_all_flags_dict_keys():
    """all_flags_dict has lowercase keys for /health JSON.

    ⚠ 这个集合是 **`/health` 的输出形状**，改它就是改对外契约。
    上下文工程 sprint 批 1-a 删掉了 `psi_clear_enabled` / `psi_compress_v2`
    （η 压缩与 ψ-A 清理整条路已删）——已 grep 确认这两个 key 除 `/health` 序列化外无消费者。
    """
    d = psi_flags.all_flags_dict()
    assert set(d.keys()) == {
        "psi_cap_enabled",
        "psi_eager_compose",
        "psi_pin_inject",
    }
    # values are bool, not str
    for v in d.values():
        assert isinstance(v, bool)

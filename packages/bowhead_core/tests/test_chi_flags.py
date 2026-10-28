"""Sprint χ Phase 0 — chi_flags env var pattern tests (mirrors psi_flags)."""

from __future__ import annotations

import pytest

from bowhead_core import chi_flags


def test_default_post_flip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider emission, concurrent execution and BI batching default on."""
    for var in ("PARALLEL_EMIT", "PARALLEL_EXEC", "BI_BATCH"):
        monkeypatch.delenv(f"BOWHEAD_CHI_{var}", raising=False)
    assert chi_flags.parallel_emit_enabled() is True  # χ-1
    assert chi_flags.parallel_exec_enabled() is True  # χ-2
    assert chi_flags.bi_batch_enabled() is True  # χ-3


def test_env_var_enables(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOWHEAD_CHI_PARALLEL_EXEC=1 → True."""
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", "1")
    monkeypatch.setenv("BOWHEAD_CHI_BI_BATCH", "1")
    assert chi_flags.parallel_exec_enabled() is True
    assert chi_flags.bi_batch_enabled() is True


def test_env_var_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only '1' enables — '0' / 'true' / empty all disable."""
    for val in ("0", "true", "yes", "", "False"):
        monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", val)
        assert chi_flags.parallel_emit_enabled() is False, f"failed for val={val!r}"


def test_runtime_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """get() re-reads env per call — mutating os.environ takes effect immediately.

    Explicit provider settings take effect on each access.
    """
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "0")
    assert chi_flags.parallel_emit_enabled() is False
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    assert chi_flags.parallel_emit_enabled() is True
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "0")
    assert chi_flags.parallel_emit_enabled() is False


def test_concurrency_helpers_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_CHI_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("BOWHEAD_CHI_GLOBAL_MAX_CONCURRENT", raising=False)
    assert chi_flags.max_concurrent() == 6
    assert chi_flags.global_max_concurrent() == 12


def test_concurrency_helpers_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_MAX_CONCURRENT", "10")
    monkeypatch.setenv("BOWHEAD_CHI_GLOBAL_MAX_CONCURRENT", "20")
    assert chi_flags.max_concurrent() == 10
    assert chi_flags.global_max_concurrent() == 20


def test_all_flags_dict_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """For /health exposure — lowercased keys, all bool.

    Provider emission can be disabled while batch execution stays enabled.
    """
    monkeypatch.setenv("BOWHEAD_CHI_BI_BATCH", "1")
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "0")  # explicit OFF
    d = chi_flags.all_flags_dict()
    assert set(d.keys()) == {
        "chi_parallel_emit",
        "chi_parallel_exec",
        "chi_bi_batch",
    }
    assert d["chi_bi_batch"] is True
    assert d["chi_parallel_emit"] is False


def test_assert_passes_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default single-worker settings permit the enabled concurrency features."""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    for var in ("PARALLEL_EMIT", "PARALLEL_EXEC", "BI_BATCH"):
        monkeypatch.delenv(f"BOWHEAD_CHI_{var}", raising=False)
    chi_flags.assert_consistent_and_single_worker()


def test_assert_aborts_on_multi_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with pytest.raises(RuntimeError, match="single worker"):
        chi_flags.assert_consistent_and_single_worker()


def test_assert_aborts_on_inconsistent_batch_without_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """BI_BATCH=1 + PARALLEL_EXEC=0 → abort."""
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("BOWHEAD_CHI_BI_BATCH", "1")
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", "0")
    with pytest.raises(RuntimeError, match="BI_BATCH=1 requires"):
        chi_flags.assert_consistent_and_single_worker()


def test_assert_passes_consistent_batch_with_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """BI_BATCH=1 + PARALLEL_EXEC=1 → ok."""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setenv("BOWHEAD_CHI_BI_BATCH", "1")
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", "1")
    chi_flags.assert_consistent_and_single_worker()  # no raise


def test_max_concurrent_non_numeric_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: non-numeric BOWHEAD_CHI_MAX_CONCURRENT → int() ValueError.

    Caller (χ-2 subagent gather) must handle this or wrap. Documenting
    behavior so future regression caught early.
    """
    monkeypatch.setenv("BOWHEAD_CHI_MAX_CONCURRENT", "abc")
    with pytest.raises(ValueError):
        chi_flags.max_concurrent()

from __future__ import annotations

import pytest
from bowhead_core.artifact_store import LocalBackend, S3Backend, build_artifact_backend_from_env


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "local", " LOCAL "])
def test_local_backend_is_default_or_explicit(monkeypatch, tmp_path, value) -> None:
    if value is None:
        monkeypatch.delenv("BOWHEAD_ARTIFACT_BACKEND", raising=False)
    else:
        monkeypatch.setenv("BOWHEAD_ARTIFACT_BACKEND", value)

    backend = build_artifact_backend_from_env(tmp_path / "sqlite" / "bowhead.db")

    assert isinstance(backend, LocalBackend)
    assert backend.root_dir == tmp_path / "artifacts"


@pytest.mark.unit
def test_unknown_backend_does_not_fall_back_to_local(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BOWHEAD_ARTIFACT_BACKEND", "s33")

    with pytest.raises(ValueError, match="must be local or s3"):
        build_artifact_backend_from_env(tmp_path / "sqlite" / "bowhead.db")


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["BOWHEAD_S3_ENDPOINT", "BOWHEAD_S3_BUCKET", "BOWHEAD_S3_ACCESS_KEY", "BOWHEAD_S3_SECRET_KEY"])
def test_incomplete_s3_config_does_not_fall_back_to_local(monkeypatch, tmp_path, missing) -> None:
    monkeypatch.setenv("BOWHEAD_ARTIFACT_BACKEND", "s3")
    for key in ("BOWHEAD_S3_ENDPOINT", "BOWHEAD_S3_BUCKET", "BOWHEAD_S3_ACCESS_KEY", "BOWHEAD_S3_SECRET_KEY"):
        monkeypatch.setenv(key, "http://storage.invalid" if key == "BOWHEAD_S3_ENDPOINT" else "test")
    monkeypatch.delenv(missing)

    with pytest.raises(ValueError, match="requires endpoint, bucket, access key and secret key"):
        build_artifact_backend_from_env(tmp_path / "sqlite" / "bowhead.db")


@pytest.mark.unit
def test_complete_s3_config_uses_s3(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BOWHEAD_ARTIFACT_BACKEND", "s3")
    for key in ("BOWHEAD_S3_ENDPOINT", "BOWHEAD_S3_BUCKET", "BOWHEAD_S3_ACCESS_KEY", "BOWHEAD_S3_SECRET_KEY"):
        monkeypatch.setenv(key, "http://storage.invalid" if key == "BOWHEAD_S3_ENDPOINT" else "test")

    backend = build_artifact_backend_from_env(tmp_path / "sqlite" / "bowhead.db")

    assert isinstance(backend, S3Backend)

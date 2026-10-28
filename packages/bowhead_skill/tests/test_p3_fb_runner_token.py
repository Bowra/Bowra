"""CCA P3 · S6.2 (F-B) — runner-side token threading.

_fetch_artifact carries the gateway-minted artifact-fetch token as Authorization: Bearer;
_materialize_inputs threads it through; the token is read HOST-SIDE and never written into
the container-visible input.json (which stays {inputs[, _sandbox_requirements]}).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_fb_runner_under_test", _RUNNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fb_runner_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _load_runner()


def test_fetch_artifact_sends_bearer_when_token(runner: ModuleType) -> None:
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"bytes"

    def _fake_urlopen(req, timeout=30):
        captured["headers"] = dict(req.header_items())
        return _Resp()

    def _fake_build_opener(*handlers, **kw):
        captured["handlers"] = handlers  # capture the positional handlers _fetch_artifact wires
        return SimpleNamespace(open=_fake_urlopen)

    # _fetch_artifact now opens via build_opener(_NoAuthOnRedirect()).open — patch that seam.
    with patch.object(runner.urllib.request, "build_opener", _fake_build_opener):
        out = runner._fetch_artifact("art_abc", "http://gw:8080", token="tok-123")
    assert out == b"bytes"
    # urllib title-cases header keys → "Authorization"; first hop to the gateway keeps the Bearer
    assert captured["headers"].get("Authorization") == "Bearer tok-123"
    # WIRING GUARD (review): the opener MUST carry _NoAuthOnRedirect — else a refactor that drops
    # it silently reintroduces the cross-host token leak with a green suite (proven false-green).
    assert any(isinstance(h, runner._NoAuthOnRedirect) for h in captured["handlers"])


def test_fetch_artifact_no_header_when_no_token(runner: ModuleType) -> None:
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"x"

    def _fake_urlopen(req, timeout=30):
        captured["headers"] = dict(req.header_items())
        return _Resp()

    with patch.object(runner.urllib.request, "build_opener", lambda *a, **k: SimpleNamespace(open=_fake_urlopen)):
        runner._fetch_artifact("art_abc", "http://gw:8080")  # no token
    assert "Authorization" not in captured["headers"]


def test_fetch_artifact_redirect_strips_authorization(runner: ModuleType) -> None:
    """review fix: following the gateway's 302 → presigned object-store URL must NOT carry
    the scoped artifact-fetch Bearer to the (different) S3/MinIO host. CPython's default
    redirect handler copies Authorization across hosts; _NoAuthOnRedirect drops it."""
    import urllib.request

    h = runner._NoAuthOnRedirect()
    req = urllib.request.Request("https://gw.internal/api/v1/files/art_1")
    req.add_header("Authorization", "Bearer scoped.jwt.token")
    req.add_header("Accept", "application/octet-stream")
    new = h.redirect_request(
        req,
        None,
        302,
        "Found",
        {},
        "https://objects.example.com:90/bucket/obj?X-Amz-Signature=abc",
    )
    assert new is not None
    # the scoped token is gone on the redirect hop …
    assert not any(k.lower() == "authorization" for k in new.headers)
    # … but other (non-credential) headers survive, and the hop targets the object-store host
    assert any(k.lower() == "accept" for k in new.headers)
    assert new.full_url.startswith("https://objects.example.com")


def test_materialize_inputs_threads_token(runner: ModuleType, tmp_path: Path) -> None:
    seen = {}

    def _fake_fetch(aid, gateway, token=None):
        seen["token"] = token
        return b"data"

    with patch.object(runner, "_fetch_artifact", _fake_fetch):
        _resolved, errs = runner._materialize_inputs(
            {"in1": {"artifact_id": "art_xyz", "filename": "f.csv"}}, tmp_path, "http://gw:8080", artifact_fetch_token="scoped-tok"
        )
    assert not errs
    assert seen["token"] == "scoped-tok"  # threaded into _fetch_artifact


def test_upload_artifact_sends_bearer_when_token(runner: ModuleType, tmp_path: Path) -> None:
    """S6 review P1: the OUTPUT upload must also carry the token so it's owned by the real
    caller — symmetric with the fetch side (else chained output→input 404s)."""
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"file_id": "art_new", "url": "/x"}'

    def _fake_urlopen(req, timeout=60):
        captured["headers"] = dict(req.header_items())
        return _Resp()

    f = tmp_path / "out.csv"
    f.write_text("a,b\n1,2\n")
    with patch.object(runner.urllib.request, "urlopen", _fake_urlopen):
        rec = runner._upload_artifact(f, "http://gw:8080", token="up-tok")
    assert rec is not None
    assert captured["headers"].get("Authorization") == "Bearer up-tok"


def test_upload_artifact_no_bearer_without_token(runner: ModuleType, tmp_path: Path) -> None:
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"file_id": "art_new"}'

    def _fake_urlopen(req, timeout=60):
        captured["headers"] = dict(req.header_items())
        return _Resp()

    f = tmp_path / "out.csv"
    f.write_text("x\n")
    with patch.object(runner.urllib.request, "urlopen", _fake_urlopen):
        runner._upload_artifact(f, "http://gw:8080")  # no token
    assert "Authorization" not in captured["headers"]

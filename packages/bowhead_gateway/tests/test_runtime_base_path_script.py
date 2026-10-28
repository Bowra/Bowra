"""Execute the emitted history patch before testing browser refresh URLs."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from bowhead_gateway.static_localized import _runtime_base_path_script


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed to execute the browser history patch")
@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
@pytest.mark.parametrize("method", ["pushState", "replaceState"])
def test_history_root_writes_keep_a_refreshable_canonical_url(prefix: str, method: str) -> None:
    html = _runtime_base_path_script(prefix, f"https://browser.example{prefix}", None)
    helpers = html[html.index("const toPublic=") : html.index("const ensureMetadataNode=")]
    patch = html[html.index('for(const m of ["pushState"') : html.index("const originalFetch=")]
    program = """
const assert = require('node:assert/strict');
const origin = 'https://browser.example';
const p = PREFIX;
let location = new URL(origin + p + '/');
const history = {};
for (const method of ['pushState', 'replaceState']) {
  history[method] = function(state, title, url) {
    const next = new URL(url, location);
    if (next.origin !== origin) throw new DOMException('Cross-origin history', 'SecurityError');
    location = next;
    this.state = state;
  };
}
const window = {history};
HELPERS
PATCH
const suffix = '?session=s_refresh&next=a%2Fb#results';
for (const target of [
  '/' + suffix, p + suffix, new URL(origin + p + suffix), origin + p + suffix,
  origin + '/' + suffix, p + '#results', origin + p + '#results',
]) {
  location = new URL(origin + p + '/');
  const state = {session: 's_refresh'};
  history[METHOD](state, '', target);
  assert.equal(location.href, origin + p + '/' + (String(target).includes('?') ? suffix : '#results'));
  assert.equal(history.state, state);
}
const signedDownload = origin + '/storage/file?signature=opaque';
assert.equal(toPublicUrl(signedDownload), signedDownload);
assert.throws(() => history[METHOD]({}, '', 'https://other.example/'), {name: 'SecurityError'});
"""
    program = program.replace("PREFIX", json.dumps(prefix)).replace("METHOD", json.dumps(method)).replace("HELPERS", helpers).replace("PATCH", patch)
    result = subprocess.run([shutil.which("node"), "-"], input=program, text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr

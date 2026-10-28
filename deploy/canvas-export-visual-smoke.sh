#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# W2 (export-runtime-hardening): build-time golden visual smoke test.
#
# Render fixed specs in the sole canonical environment (app image: fonts/Chromium/DPI all pinned),
# then compare golden output against committed baseline PNGs with thresholds. Catch two failure classes:
#   1. deck-12pt-style regressions: functional checks green, visual appearance regresses;
#   2. Broken rendering runtime (missing fonts/browser cannot start/component crashes): fail the build rather than
#      silently degrading V2 after deployment.
# Run pixel assertions only inside the image, never on local/CI hosts (no cross-environment pixel flakes).
#
# Usage (inside the image):
#   canvas-export-visual-smoke            # Verify: render → compare against baseline
#   canvas-export-visual-smoke --regenerate  # Regenerate baseline (write to GOLDEN_DIR)
#
# Baseline update policy (tech-design §2.2): regenerate baseline PNGs only for ① EXPORT_VISUAL_RENDERER_VERSION
# bumps or ② intentional visual redesigns via --regenerate, committing them together; no manual screenshots.
#
# SHA authority: never store SHA in fixtures; compute it using canonical Python inside the image
# (bowhead_core.export_visual_identity, paired with FE test_export_visual_identity_parity
# cross-runtime parity guard) on demand, with zero drift.
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

MODE="${1:-check}"
GOLDEN_DIR="${GOLDEN_DIR:-/app/deploy/export-goldens}"
STATIC_DIR="${STATIC_DIR:-/app/frontend-static/zh-CN}"
RUNTIME_DIR="${RUNTIME_DIR:-/app/export-visual-runtime}"
VENV_PY="${VENV_PY:-/app/.venv/bin/python}"
NODE_BIN="${NODE_BIN:-/usr/local/bin/node}"
PORT="${SMOKE_PORT:-18123}"
WORK="$(mktemp -d)"
trap 'kill "${SERVER_PID:-0}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

[ -d "$STATIC_DIR" ] || { echo "ERR: static dir missing: $STATIC_DIR"; exit 1; }
[ -d "$GOLDEN_DIR" ] || { echo "ERR: golden dir missing: $GOLDEN_DIR"; exit 1; }

# Static frontend = Gateway runtime output; http.server suffices (all pages hydrate client-side).
python3 -m http.server "$PORT" --directory "$STATIC_DIR" --bind 127.0.0.1 >/dev/null 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  if "$VENV_PY" - <<PY 2>/dev/null
import urllib.request
urllib.request.urlopen("http://127.0.0.1:$PORT/", timeout=1)
PY
  then break; fi
  sleep 0.2
done

FAIL=0
for fixture in "$GOLDEN_DIR"/spec_*.json; do
  name="$(basename "$fixture" .json)"
  baseline="$GOLDEN_DIR/$name.png"
  out="$WORK/$name.png"
  req="$WORK/$name.request.json"

  # Construct request + compute SHA on demand (canonical Python)
  "$VENV_PY" - "$fixture" "$req" "$PORT" "$GOLDEN_DIR" <<'PY'
import json, sys
from pathlib import Path

from bowhead_core.chart_style_export import parse_chart_style_export_theme
from bowhead_core.export_visual_identity import compute_visual_input_sha256, EXPORT_VISUAL_RENDERER_VERSION
from bowhead_core.print_theme import current_print_theme_snapshot

fixture_path, req_path, port, golden_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
fx = json.load(open(fixture_path, encoding="utf-8"))
# renderer v11 起 theme 进入视觉身份（`theme_hash`），且导出路由只收完整的 reviewed
# preset 包（FE `parseChartStyleExportTheme` 是精确 key 匹配，旧的 {"kind":"default"}
# 一律拒收）。基线用的 theme 因此和 PNG 一样属于基线本身，钉在 GOLDEN_DIR/theme.json。
# 这里过一遍正典解析器：preset 的 revision / checksum / 内容 sha 任一漂移就当场炸，
# 不会静默换一套配色再去比像素。
locale = "zh-CN"
theme = parse_chart_style_export_theme(
    json.loads(Path(golden_dir, "theme.json").read_text(encoding="utf-8"))
)
print_theme = current_print_theme_snapshot()
sha = compute_visual_input_sha256(
    section_kind=fx["sectionKind"], payload=fx["payload"], locale=locale,
    theme=theme, print_theme=print_theme,
)
if not sha:
    raise SystemExit(f"visual sha unavailable for kind={fx['sectionKind']}")
request = {
    "frontendBaseUrl": f"http://127.0.0.1:{port}",
    "locale": locale,
    "timezone": "Asia/Shanghai",
    "timeoutMs": 45000,
    "payload": {
        "blockKey": fx["blockKey"],
        "sectionKind": fx["sectionKind"],
        "payload": fx["payload"],
        "locale": locale,
        "theme": theme,
        "printTheme": print_theme,
        "visualInputSha256": sha,
        "rendererVersion": EXPORT_VISUAL_RENDERER_VERSION,
    },
}
json.dump(request, open(req_path, "w", encoding="utf-8"), ensure_ascii=False)
PY

  if ! (cd "$RUNTIME_DIR" && "$NODE_BIN" scripts/render-export-visual.mjs "$req" "$out" >/dev/null); then
    echo "SMOKE FAIL: $name — render failed（装配坏：字体/浏览器/组件）"
    FAIL=1
    continue
  fi

  if [ "$MODE" = "--regenerate" ]; then
    cp "$out" "$baseline"
    echo "regenerated: $baseline"
    # R7: persist the renderer version in the manifest on regeneration. This policy
    # (version bumps require baseline regeneration) relied on memory; three unnoticed violations since 2026-08-01
    # (v2→v3 / v3→v4 / v4→v5) surfaced only when builds failed. With this file,
    # test_export_goldens_renderer_version.py detects stale baselines immediately **on the local host**,
    # without waiting for the image's pixel gate.
    "$VENV_PY" - "$GOLDEN_DIR" <<'PY'
import sys
from pathlib import Path
from bowhead_core.export_visual_identity import EXPORT_VISUAL_RENDERER_VERSION
Path(sys.argv[1], "RENDERER_VERSION").write_text(EXPORT_VISUAL_RENDERER_VERSION + "\n", encoding="utf-8")
PY
    continue
  fi

  [ -f "$baseline" ] || { echo "SMOKE FAIL: $name — 缺基线 ${baseline}（先 --regenerate 并提交）"; FAIL=1; continue; }

  # Compare: dimensions exactly equal + ≤16 changed pixels (any channel Δ>8 counts as changed).
  # Rendering is empirically deterministic (same spec twice with export animations disabled: zero pixel differences); near-zero threshold:
  # Changing one title word moves only ~158px (0.011%); ratio/RMSE thresholds miss such local regressions.
  # AA drift from Chromium/font upgrades fails visibly: precisely the policy's occasion for
  # review + --regenerate, rather than noise to absorb.
  if ! "$VENV_PY" - "$baseline" "$out" "$name" <<'PY'
import sys
from PIL import Image, ImageChops
base, cur, name = Image.open(sys.argv[1]).convert("RGB"), Image.open(sys.argv[2]).convert("RGB"), sys.argv[3]
if base.size != cur.size:
    print(f"SMOKE FAIL: {name} — size {cur.size} != baseline {base.size}")
    raise SystemExit(1)
diff = ImageChops.difference(base, cur)
r, g, b = diff.split()
mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
changed = sum(count for value, count in enumerate(mx.histogram()) if value > 8)
bbox = diff.getbbox()
if changed > 16:
    print(f"SMOKE FAIL: {name} — {changed} changed pixels > 16, diff bbox={bbox}")
    raise SystemExit(1)
print(f"ok: {name} changed pixels = {changed}")
PY
  then
    FAIL=1
  fi
done

exit "$FAIL"

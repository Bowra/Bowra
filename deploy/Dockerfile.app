# Bowra Community application image: FastAPI gateway + Next.js static export.

ARG DOCKER_REGISTRY=

# Build frontend static assets.
FROM ${DOCKER_REGISTRY}library/node:24.22.1-bookworm-slim AS frontend-builder

WORKDIR /build
# Copy manifests first to retain the npm cache layer.
COPY packages/bowhead_frontend/package.json packages/bowhead_frontend/package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN --mount=type=cache,target=/root/.npm \
    npm ci --registry="${NPM_REGISTRY}" --no-audit --no-fund --prefer-offline

# Canvas Export V2 runs the real Canvas visual components in an isolated
# Chromium route. Pin the browser cache path so the exact build-time binary can
# be copied into the Python runtime image without a runtime download.
# W1 (export-runtime-hardening): cdn.playwright.dev reaches only ~25KB/s on hosts in China;
# with a cold cache, downloading the 112MiB Chrome Headless Shell takes ~75 minutes. The mirror-fetch shim
# prefetches from npmmirror (measured ~2MB/s) and starts a local HTTP server for the official installer
# (still the sole layout/marker authority); failed prefetch automatically falls back to the official CDN, never making things worse.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY deploy/scripts/playwright-mirror-fetch.mjs /tmp/playwright-mirror-fetch.mjs
RUN set -e; \
    node /tmp/playwright-mirror-fetch.mjs /tmp/pw-mirror & \
    for i in $(seq 1 120); do [ -f /tmp/pw-mirror/ready ] && break; sleep 1; done; \
    if [ -f /tmp/pw-mirror/port ]; then \
      export PLAYWRIGHT_DOWNLOAD_HOST="http://127.0.0.1:$(cat /tmp/pw-mirror/port)"; \
      echo "==> playwright install via mirror shim ($PLAYWRIGHT_DOWNLOAD_HOST)"; \
    else \
      echo "==> playwright install via default CDN (mirror shim unavailable)"; \
    fi; \
    node node_modules/playwright/cli.js install --only-shell chromium \
      || { echo "==> mirror-assisted install failed; retrying via default CDN"; \
           unset PLAYWRIGHT_DOWNLOAD_HOST; \
           node node_modules/playwright/cli.js install --only-shell chromium; }; \
    rm -rf /tmp/pw-mirror /tmp/playwright-mirror-fetch.mjs

COPY packages/bowhead_frontend/ ./
RUN node -e "if (require('./build-manifest.json').edition !== 'community') process.exit(1)"
# Help Center Markdown remains repository-owned source content. Copy it beside
# the frontend package so static export can embed every article and diagram.
COPY docs/help/ ./docs/help/
# Third-party notices are published with the static export (Help › About links to them);
# npm prebuild (scripts/copy-legal-notices.mjs) copies this file to public/legal/.
COPY THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md
# Shared repository-root contract: `e2e/_composer-reference-fixtures.ts` directly uses
# `import "../../../contracts/chat-references/input-document-v1.json"`; this relative path
# points from `packages/bowhead_frontend/e2e/` to the **repository root**, but the frontend is relocated to `/build` here.
# Three parent traversals reach the image root, so contracts must live at `/contracts`; otherwise `next build` typechecking
# (which includes e2e) reports "Cannot find module".
# Observed 2026-09-09: main could not build after `fc70efdc6` added this import, while local
# `npm run typecheck` passed (contracts/ exists at repository root). Such image-only failures
# escape local gates and surface only during the build.
COPY contracts/ /contracts/
COPY config/ /config/
# Build each locale separately; the gateway selects the matching static export.
# en-US (the default language) runs the full lint and type checks; the other locales
# set BOWHEAD_SKIP_BUILD_CHECKS=1 because those results do not depend on the locale.
ARG BOWHEAD_VERSION=dev
# Force invalidation of Next.js build cache when requested.
ARG CACHEBUST=1
ENV NEXT_STATIC_EXPORT=1 \
    NEXT_TELEMETRY_DISABLED=1 \
    NODE_OPTIONS=--max-old-space-size=3072
RUN echo "cachebust=$CACHEBUST version=$BOWHEAD_VERSION" \
    && rm -rf .next out out-zh-CN out-en-US out-de-DE out-fr-FR \
    && NEXT_PUBLIC_BOWHEAD_SSE_MODE=sse NEXT_PUBLIC_BOWHEAD_VERSION=$BOWHEAD_VERSION NEXT_PUBLIC_BUILD_LOCALE=en-US npm run build \
    && mv out out-en-US \
    && rm -rf .next \
    && BOWHEAD_SKIP_BUILD_CHECKS=1 NEXT_PUBLIC_BOWHEAD_SSE_MODE=sse NEXT_PUBLIC_BOWHEAD_VERSION=$BOWHEAD_VERSION NEXT_PUBLIC_BUILD_LOCALE=zh-CN npm run build \
    && mv out out-zh-CN \
    && rm -rf .next \
    && BOWHEAD_SKIP_BUILD_CHECKS=1 NEXT_PUBLIC_BOWHEAD_SSE_MODE=sse NEXT_PUBLIC_BOWHEAD_VERSION=$BOWHEAD_VERSION NEXT_PUBLIC_BUILD_LOCALE=de-DE npm run build \
    && mv out out-de-DE \
    && rm -rf .next \
    && BOWHEAD_SKIP_BUILD_CHECKS=1 NEXT_PUBLIC_BOWHEAD_SSE_MODE=sse NEXT_PUBLIC_BOWHEAD_VERSION=$BOWHEAD_VERSION NEXT_PUBLIC_BUILD_LOCALE=fr-FR npm run build \
    && mv out out-fr-FR

# Build runtime image.
ARG DOCKER_REGISTRY=
FROM ${DOCKER_REGISTRY}library/python:3.12-slim-bookworm AS runtime

ARG DEBIAN_MIRROR=http://mirrors.tencent.com/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.tencent.com/debian-security
# The docs cleanup keeps each Debian package's /usr/share/doc/<pkg>/copyright:
# the image ships GPL components (git, poppler-utils, fonts-wqy-zenhei, ...)
# whose license texts must travel with it. The build fails if they go missing.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    echo "deb ${DEBIAN_MIRROR} bookworm main contrib non-free non-free-firmware" > /etc/apt/sources.list \
    && echo "deb ${DEBIAN_SECURITY_MIRROR} bookworm-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libmagic1 \
        git \
        fonts-noto-cjk \
        fonts-wqy-zenhei \
        fontconfig \
        libreoffice-calc \
        libreoffice-impress \
        poppler-utils \
    && fc-cache -f \
    && find /usr/share/doc -mindepth 2 ! -path '/usr/share/doc/*/copyright' ! -type d -delete \
    && find /usr/share/doc -mindepth 1 -type d -empty -delete \
    && rm -rf /usr/share/man \
    && test -s /usr/share/doc/git/copyright \
    && test -s /usr/share/doc/poppler-utils/copyright

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Minimal Canvas visual-render runtime. The app image deliberately does not
# carry the complete frontend dependency tree.
COPY --from=frontend-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend-builder /build/node_modules/playwright ./export-visual-runtime/node_modules/playwright
COPY --from=frontend-builder /build/node_modules/playwright-core ./export-visual-runtime/node_modules/playwright-core
COPY --from=frontend-builder /build/scripts/render-export-visual.mjs ./export-visual-runtime/scripts/render-export-visual.mjs
COPY --from=frontend-builder /ms-playwright /ms-playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN node /app/export-visual-runtime/node_modules/playwright/cli.js install-deps chromium

# README.md is required by the root project metadata.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --all-packages --frozen --no-dev --no-install-workspace

# Copy Python workspace members individually to preserve the dependency cache.
COPY packages/bowhead_core/      ./packages/bowhead_core/
COPY packages/bowhead_router/    ./packages/bowhead_router/
COPY packages/bowhead_gateway/   ./packages/bowhead_gateway/
COPY packages/bowhead_memory/    ./packages/bowhead_memory/
COPY packages/bowhead_skill/     ./packages/bowhead_skill/
COPY packages/bowhead_bi_bridge/ ./packages/bowhead_bi_bridge/
COPY packages/bi_mock/           ./packages/bi_mock/
COPY packages/bowhead_cli/       ./packages/bowhead_cli/
COPY config/ ./config/
ENV UV_DEFAULT_INDEX=https://mirrors.tencent.com/pypi/simple/ \
    UV_HTTP_TIMEOUT=120
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --all-packages --frozen --no-dev

# Pre-bake the tiktoken cl100k_base BPE vocab into the image. token_estimator.py
# (`tiktoken.get_encoding("cl100k_base")`) is hit on every Lead turn's token-budget
# math; without this, a fresh container downloads ~1.7MB from
# openaipublic.blob.core.windows.net on the first turn — slow/failing under
# restricted egress. Baking makes token estimation deterministic + fully offline.
# TIKTOKEN_CACHE_DIR must be set before the warmup so the file lands here, and it
# persists to runtime so the cached blob is read instead of re-downloaded.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN mkdir -p /opt/tiktoken \
    && /app/.venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && test -n "$(ls -A /opt/tiktoken)"  # fail-closed: warmup must have cached the vocab

COPY deploy/canvas-export-image-smoke.sh /usr/local/bin/canvas-export-image-smoke
COPY deploy/start-app.sh /usr/local/bin/bowhead-start-app
COPY scripts/memory_privacy_prepare.py /usr/local/bin/bowhead-memory-privacy-prepare
RUN chmod 0755 /usr/local/bin/canvas-export-image-smoke \
        /usr/local/bin/bowhead-start-app \
        /usr/local/bin/bowhead-memory-privacy-prepare \
    && /usr/local/bin/canvas-export-image-smoke

COPY --from=frontend-builder /build/out-zh-CN /app/frontend-static/zh-CN
COPY --from=frontend-builder /build/out-en-US /app/frontend-static/en-US
COPY --from=frontend-builder /build/out-de-DE /app/frontend-static/de-DE
COPY --from=frontend-builder /build/out-fr-FR /app/frontend-static/fr-FR

# W2 (export-runtime-hardening): include smoke script and golden baselines in the image; RUN follows USER
# bowhead (Chromium runs only as non-root, with the same permissions as runtime exports).
COPY deploy/canvas-export-visual-smoke.sh /usr/local/bin/canvas-export-visual-smoke
COPY deploy/export-goldens/ /app/deploy/export-goldens/
RUN chmod 0755 /usr/local/bin/canvas-export-visual-smoke

# Copy version after expensive layers to preserve their cache.
COPY VERSION ./VERSION
ARG BOWHEAD_VERSION=dev
RUN printf 'reader=0002\nwriter=0002\n' > /app/AGENT_MARKETPLACE_COMPATIBILITY

ENV BOWHEAD_FRONTEND_STATIC_DIR=/app/frontend-static \
    BOWHEAD_EXPORT_FRONTEND_DIR=/app/export-visual-runtime \
    BOWHEAD_EXPORT_FRONTEND_BASE_URL=http://127.0.0.1:8080 \
    BOWHEAD_EXPORT_NODE_PATH=/usr/local/bin/node \
    BOWHEAD_EXPORT_PDFTOPPM_PATH=/usr/bin/pdftoppm \
    BOWHEAD_EXPORT_SOFFICE_PATH=/usr/bin/soffice \
    BOWHEAD_ENV=prod \
    BOWHEAD_VERSION=${BOWHEAD_VERSION} \
    BOWHEAD_LOG_FORMAT=json \
    BOWHEAD_DB_URL=sqlite+aiosqlite:////app/.data/sqlite/bowhead.db \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    UV_FROZEN=1 \
    PATH="/app/.venv/bin:${PATH}"

RUN mkdir -p /app/.data/sqlite /app/.data/artifacts

RUN useradd -u 1000 -m -s /bin/bash bowhead
# Recursive chown would duplicate the whole /app layer in OverlayFS (~562 MB).
# Only these runtime directories must be writable by the non-root process.
RUN chown -R bowhead:bowhead /app/.data
USER bowhead

# W2 (export-runtime-hardening): build-time golden visual smoke in the sole canonical rendering environment
# (this image: pinned fonts/Chromium/DPI), comparing fixed specs with committed baselines (RMSE≤3%,
# dimensions strictly equal). Catches visual regressions despite passing functionality and broken renderer assembly; failure means build
# failure and no deployment. Update baselines only with an EXPORT_VISUAL_RENDERER_VERSION bump or intentional visual changes;
# regenerate with canvas-export-visual-smoke --regenerate and commit together (no manual screenshots).
# After VERSION COPY, so every build reruns it (~40s), as the gate intends.
RUN /usr/local/bin/canvas-export-visual-smoke

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

# --timeout-keep-alive 75 (default 5s): prod connects directly to uvicorn without a reverse proxy; 5s idle closure races with client
# connection-pool reuse (httpx also defaults keepalive_expiry to 5.0s), so idle≈5-6s
# polling inevitably causes intermittent RemoteProtocolError (follow-up bugs 0710 ③; deterministic probe reproduced 50%).
# 75s follows nginx keepalive_timeout conventions; long SSE streams already have 15s keepalive heartbeats and remain unaffected.
CMD ["/usr/local/bin/bowhead-start-app"]

# Build Bowra from source

English | [简体中文](README.zh-CN.md)

Use Python 3.12, `uv`, Node.js 24 and npm. Run these commands from this independent Community checkout. The committed Gateway and frontend manifests must both select `community`; edition selection is a build input.

## Install and configure

```sh
make install
make install-frontend
cp .env.example .env
```

Generate instance keys once, before the first start. This writes keys to the private `.env` file without printing them:

```sh
uv run python - <<'PYTHON'
from pathlib import Path
import secrets
from cryptography.fernet import Fernet

path = Path('.env')
text = path.read_text()
for name, value in {
    'BOWHEAD_FERNET_KEY': Fernet.generate_key().decode(),
    'BOWHEAD_JWT_SECRET': secrets.token_urlsafe(48),
}.items():
    placeholder = name + '=\n'
    if placeholder not in text:
        raise SystemExit(f'{name} is already configured; preserve existing keys')
    text = text.replace(placeholder, name + '=' + value + '\n', 1)
path.write_text(text)
path.chmod(0o600)
PYTHON
```

Review `.env`. Its relative database path is `.data/sqlite/bowhead.db`; keep the working directory stable. Retain both keys and all database/object data for upgrades and recovery. Restoring an instance snapshot restores its account state; a snapshot from before deletion can restore that account. Do not reuse a Business database for first-owner setup.

## Runtime prerequisites

Before starting the Gateway, configure a reachable, compatible sandbox executor in `.env` using both `BOWHEAD_SANDBOX_EXECUTOR_URL` and `BOWHEAD_SANDBOX_EXECUTOR_TOKEN`. Startup refuses missing executor configuration. Obtain the token from the executor operator; rotating it there requires updating this deployment, and an invalid token prevents execution. This source preview does not publish a supported sandbox image or a complete runtime deployment.

Workflows that invoke sandbox execution also require `BOWHEAD_SKILL_WORKDIR_ROOT` to identify the same writable directory at the same absolute path for the Gateway, executor and Docker host. The executor mounts per-run children into its sandbox. A reachable remote endpoint or an SSH tunnel does not establish this shared filesystem. Verify actual execution, isolation and downloaded output before enabling these workflows. Native `bowra-docx`, `bowra-pdf`, `bowra-pptx` and `bowra-xlsx` document Skills run in the application environment with their installed dependencies; they do not require the sandbox shared directory.

## Build and start

```sh
make typecheck-frontend
make build-frontend
make lint
make run-gateway
```

The frontend build defaults to English. The application image build in `deploy/Dockerfile.app` assembles all supported interface locales; the source command above builds one locale for local development. A saved language choice remains authoritative; unset interface preferences follow the browser/system language where a matching build exists.

`make run-gateway` exports the private `.env`, checks installation provenance, runs the supported migration/backfill preparation and then starts Uvicorn on port 8080. Open `http://localhost:8080/activate/`. In another terminal, use the same checkout and configuration to retrieve the pending setup proof:

```sh
uv run python -m bowhead_gateway.installation setup-token
```

Complete [Community initialization](community-setup.md), sign in, and configure Team model credentials. Model services, data sources, email delivery and MCP connections have their own credentials and permissions. Community has no commercial activation or user-count cap; account permissions and configured usage budgets still apply.

For system email, configure an SMTP relay and an explicit `BOWHEAD_SMTP_FROM` address that the relay authorizes, or save the Team Email Service configuration with an authorized sender address. Community provides no default sender domain. Missing sender configuration prevents delivery. Chat-initiated email requires the Team configuration.

For frontend development with hot reload, use `make run-frontend` against the running Gateway. It proxies API requests to port 8080. If you change the Gateway port, update `BOWHEAD_GATEWAY_URL`, `BOWHEAD_EXPORT_FRONTEND_BASE_URL` and the development proxy target to match. These are internal callback addresses; the export renderer must be able to reach the built frontend.

## Execution runtimes and releases

Canvas exports use a separate local rendering toolchain: Node.js, Poppler (`pdftoppm`), LibreOffice (`soffice`, for PowerPoint and spreadsheet exports), the locked frontend npm dependencies and Playwright Chromium. Install system packages for your operating system, then install the browser from the frontend package:

```sh
cd packages/bowhead_frontend
npx playwright install chromium
```

On Linux, install the browser’s required system libraries as well, using the documented Playwright system dependencies for your distribution. The readiness endpoint checks selected binary and script paths; it does not prove that the browser is installed or that an export succeeds. Test an actual PDF, PowerPoint and spreadsheet export with your configured toolchain. Local Canvas rendering and sandbox Skill execution have different dependencies.

The application Dockerfile can build an application image from this source with `docker build -f deploy/Dockerfile.app -t bowra-app:local .`. An application image is only one deployment component; this command does not build a sandbox, publish an image or establish a production deployment. Runtime composition must use independently reviewed components and persistent configuration.

## Upgrade an existing installation

Stop every Gateway, worker and other writer before the first upgraded start. Preserve the complete persistent data root, all existing SQLite files and adjunct vector data, object files, instance configuration and both instance keys. Account state follows the recovery snapshot; a snapshot taken before deletion can restore that account. Test the upgrade on an isolated recovery copy first.

This release advances the complete immutable migration chain through `0227_observed_continuation_inputs`. For installations already on 1.6.80, the coordinator upgrades the existing twelve-file SQLite layout in place. Earlier installations still go through the coordinated split, retaining the seven knowledge files and separating execution, content, notifications and observability from core. It creates a persistent automatic backup of the registered SQLite databases and available layout metadata before pending upgrades and resumes interrupted work forward. Keep complete recovery snapshots separately, including external Chroma storage, stored objects and instance configuration. All writers must remain stopped until it completes. Ensure enough disk space for the full archive and staged copies.

Use `make run-gateway` with the configured source installation. Its installation preparation admits the original provenance, runs the complete migration coordinator and records the edition before Gateway startup. For an offline preparation, use `uv run python -m bowhead_gateway.installation prepare --migration-command "$PWD/.venv/bin/bowhead"` with the same environment. Do not stamp a revision, migrate one file in isolation, issue manual SQL, or run an older application against the new layout. Keep the original complete backup. Rollback restores its matching application, all database/layout files and stored objects together, with the account state from that recovery point. Also preserve an independently configured external Chroma directory with its matching recovery point.

The application build uses Node.js 24.22.1 and the frozen frontend lock. Sandbox source and versions retain the separately reviewed Community baseline. This source preview supplies no qualified runtime image: validate the operator-provided executor, its shared writable directory and isolation separately before using sandbox-backed workflows.

## Development checks

See [Contributing](../CONTRIBUTING.md). Use focused tests for the component you change; keep the generated theme registry consistent with `config/theme_surface_coverage.yaml` and retain third-party notices. Back up the original recovery set before upgrading an initialized installation.

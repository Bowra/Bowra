import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_image_build_runs_lint_and_type_checks_exactly_once_on_en_us_first() -> None:
    dockerfile = (_REPO_ROOT / "deploy/Dockerfile.app").read_text(encoding="utf-8")
    builds = [line for line in dockerfile.splitlines() if "npm run build" in line]
    locales = [re.search(r"NEXT_PUBLIC_BUILD_LOCALE=(\S+) npm run build", line).group(1) for line in builds]
    checked = [locale for line, locale in zip(builds, locales) if "BOWHEAD_SKIP_BUILD_CHECKS=1" not in line]

    # Checks run once, on the default language, and first so a failure stops the build early.
    assert checked == ["en-US"]
    assert locales[0] == "en-US"
    assert len(locales) == len(set(locales)) > 1


def test_skip_switch_only_disables_lint_and_type_checks() -> None:
    config = (_REPO_ROOT / "packages/bowhead_frontend/next.config.mjs").read_text(encoding="utf-8")

    assert 'process.env.BOWHEAD_SKIP_BUILD_CHECKS === "1"' in config
    assert "eslint: { ignoreDuringBuilds: true }" in config
    assert 'tsconfigPath: edition === "business" ? "tsconfig.production.json" : "tsconfig.json"' in config
    assert "skipBuildChecks ? { ignoreBuildErrors: true }" in config


def test_runtime_image_requires_the_independent_test_typecheck() -> None:
    dockerfile = (_REPO_ROOT / "deploy/Dockerfile.app").read_text(encoding="utf-8")
    checker = dockerfile.split("AS frontend-typechecker", maxsplit=1)[1].split("FROM frontend-deps AS frontend-builder", maxsplit=1)[0]
    assert checker.index("next typegen") < checker.index("npm run typecheck:tests") < checker.index("&& touch /frontend-types-checked")
    runtime = dockerfile.split(" AS runtime", maxsplit=1)[1]
    assert "COPY --from=frontend-typechecker /frontend-types-checked /app/.frontend-types-checked" in runtime


def test_locale_rebuilds_keep_only_their_own_compilation_cache(tmp_path: Path) -> None:
    dockerfile = (_REPO_ROOT / "deploy/Dockerfile.app").read_text(encoding="utf-8")
    recipe = next(block for block in dockerfile.split("\n\n") if "RUN --mount=type=cache,id=bowhead-next-business-" in block)
    assert "${TARGETARCH}" in recipe and "sharing=locked" in recipe
    recipe = recipe[recipe.index('echo "cachebust=') :].replace("\\\n", " ")
    cache = tmp_path / "cache"
    recipe = recipe.replace("/next-cache", str(cache))
    # Substitute the compiler, but execute the real image cleanup/cache recipe.
    # A rebuild must remove stale pages while keeping isolated compiler state.
    compiler = """
set -eu
CACHEBUST=test BOWHEAD_VERSION=test
npm() {
    if [ "$2" = assemble ]; then
        if [ ! -f .assembled ]; then rm -rf .next; touch .assembled; fi
        return
    fi
    test ! -e .next/stale-output || return 1
    test -L .next/cache || return 1
    mkdir -p out
    test ! -e out/stale-page || return 1
    printf '%s\\n' "$NEXT_PUBLIC_BUILD_LOCALE" >> .next/cache/runs
    printf '%s\\n' "$NEXT_PUBLIC_BUILD_LOCALE" > out/locale
    touch .next/stale-output
}
"""
    for _ in range(2):
        (tmp_path / "out").mkdir(exist_ok=True)
        (tmp_path / "out/stale-page").touch()
        subprocess.run(["/bin/sh", "-c", compiler + recipe], cwd=tmp_path, check=True, capture_output=True, text=True)
        for locale in ("en-US", "zh-CN", "de-DE", "fr-FR"):
            assert (tmp_path / f"out-{locale}/locale").read_text().strip() == locale
    for locale in ("en-US", "zh-CN", "de-DE", "fr-FR"):
        assert (cache / locale / "runs").read_text().splitlines() == [locale, locale]

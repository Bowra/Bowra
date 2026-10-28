# Contributing to Bowra

English | [简体中文](CONTRIBUTING.zh-CN.md)

Bowra is a Community source preview. Read the [README](README.md) for supported scope and [Community setup](deploy/community-setup.md) for initialization requirements.

## Propose a change

1. Check the existing repository content and related discussions before starting.
2. Keep each change focused. Explain the problem, intended result, and any affected user workflow.
3. Use a topic branch and a pull request for review, following the repository's actual access and branch rules. Do not assume direct pushes to `main` are permitted.
4. Describe what you changed and how you checked it. If runtime source or a required dependency is unavailable, state that limitation instead of claiming a test passed.

Maintainers decide when a change is ready to merge. A documentation update does not establish that a feature has shipped or that a release has passed its checks.

## Check a contribution

Use Python 3.12 with `uv` and the Node version required by the frontend package. Install the committed locks with `make install` and `make install-frontend`. Initialization, model credentials and document execution require the configuration described in the setup guide.

- Python changes: run the smallest relevant `uv run pytest packages/<package>/tests/...` scope, then `make lint`.
- Frontend changes: run `make typecheck-frontend`, relevant tests with `npm test -- <test-path>` inside `packages/bowhead_frontend`, and `make build-frontend` when changing routing or build composition.
- Documentation changes: verify local links, language pairs and actual feature availability; no application tests are required.
- Run `make fmt` for formatting and inspect its diff before committing. Do not include unrelated formatting changes.

The frontend assembles its fixed Community edition before its normal build, test and typecheck commands. Keep generated outputs out of commits. Never replace the edition manifest with an environment variable or runtime setting.

## Language and documentation

- Write code, comments, commit subjects/bodies, branch names, and pull-request titles/descriptions in English.
- Use English as the default documentation entry. Maintain important Chinese editions separately and link each language pair.
- Update both editions when changing paired documentation. Keep localization strings, user content, and required protocol or persisted values in their original language where necessary.
- Verify local links against the files actually available in this repository. Do not link readers to unavailable internal documents.

## Keep contributions reviewable

Avoid unrelated formatting or package renames. Never include credentials, customer data, private source history, or materials without redistribution rights. Use synthetic examples when demonstrating a workflow.

Follow the [security reporting policy](SECURITY.md) for vulnerabilities. Keep sensitive details out of general issues and pull requests.

## License and release status

Follow the repository's [MIT License](LICENSE) and retain applicable third-party notices. Changes to licensing, repository visibility, and artifact publication require maintainer authorization. Business extensions are maintained separately and are not part of this source tree.

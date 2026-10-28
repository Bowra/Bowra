# Bowra contributor instructions

Bowra is the Community edition of Bowhead. Read [README](README.md) for the current release status and [Contributing](CONTRIBUTING.md) for the contribution workflow.

## Scope and accuracy

- Work from the files and supported workflows actually present in this repository. Do not assume access to a private source repository, internal services, or unreleased packages.
- During release preparation, distinguish planned capabilities from runnable features. Do not invent setup commands, test outcomes, release versions, or license terms.
- Keep patches focused and preserve existing user changes. Explain material limitations in the handoff.

## Language

- Use English for code, identifiers, comments, docstrings, logs, errors, tests, commit messages, branch names, and pull-request descriptions.
- Maintain repository agent instructions only in English in `AGENTS.md`; do not create localized copies.
- Default development documentation to English. Maintain paired Chinese documentation in the same change and preserve required localization and compatibility literals.
- Use Bowra for the Community product's display name, including visible wordmarks and accessible labels. Bowra shares the existing logo symbol with Bowhead; preserve historical attribution and legal notices.
- Do not rename packages, API paths, or persisted identifiers solely to match the Bowra project name.

## Validation and delivery

- Inspect the actual build and test configuration before selecting commands. Run checks appropriate to the change and report their results accurately.
- For documentation changes, check language pairs, local links, and release-status claims. No application tests are required for a documentation-only change.
- Use the contribution and branch rules documented in this repository. Do not assume permission to push directly to `main`, change visibility, publish a release, or upload an artifact.
- Never include credentials, customer data, private Git history, or unreviewed third-party materials. Source access does not grant redistribution rights.

For settings changes, maintain registered DTO descriptions, constraints and guidance with their reader/writer. Preserve typed values, explicit reset semantics, real-user authorization and original Review/Apply boundaries. Update `config/configuration-coverage.json` for pages, configuration units and model DTO fields; `make lint` checks that inventory. Do not duplicate live field lists or limits in prompts or Skills.

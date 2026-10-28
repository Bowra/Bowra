# Bowra document Skills

English | [简体中文](bowra-document-skills.zh-CN.md)

Four independently authored document packages provide a distributable source alternative to the current restricted catalog bundles.

| Package                           | Supported creation                                | Supported inspection                        |
| --------------------------------- | ------------------------------------------------- | ------------------------------------------- |
| [bowra-docx](bowra-docx/SKILL.md) | Editable headings, paragraphs, bullets and tables | Ordered main-body text and top-level tables |
| [bowra-pdf](bowra-pdf/SKILL.md)   | Paginated text/table reports                      | Embedded text by page; no OCR               |
| [bowra-pptx](bowra-pptx/SKILL.md) | Editable 16:9 title/bullet/table decks            | Top-level slide text and tables             |
| [bowra-xlsx](bowra-xlsx/SKILL.md) | Value/date workbooks, formats and filters         | Values and labeled unevaluated formula text |

Each package includes English instructions, a closed JSON schema, one standalone CLI, a synthetic example, evaluation prompts, tested direct requirements, MIT LICENSE and provenance. The scripts call public libraries; they do not import the old Skill packages or private Bowhead modules.

## Try an example

From a synchronized workspace with existing dependencies:

```bash
uv run --no-sync python packages/bowhead_skill/catalog/bowra-docx/scripts/run.py \
  --input packages/bowhead_skill/catalog/bowra-docx/examples/create.json \
  --workdir /tmp/bowra-docx-example
```

Use a fresh output directory. Change both package paths to another package to run its example. Outside this workspace, create a Python 3.12 environment and install that package's `requirements.txt`; no dependency is downloaded during script execution.

Creation returns the actual filename, size and SHA-256. To inspect a file, stage an authorized copy below the workdir and supply `{"operation":"inspect","source":"inputs/source.docx"}`. See each package's reference for budgets and unsupported objects.

## Integration status

These are standard built-in Skills under `catalog/`, discovered by the existing loader with their distinct `bowra-*` names and subject to current enablement and exact-resource grants. No default resource binding, Tool privilege or existing Agent release is changed. These scripts follow the existing `--input/--workdir` protocol; Artifact registration and source staging belong to the invoking platform.

The existing restricted packages remain private exclusions. A future Community source assembly must explicitly select these packages, review exact hashes/materials, update Agent bindings with owner approval and validate the installed dependency closure. Sandbox code and images are unchanged: replacing source packages alone does not remove helpers already copied into an image.

The basic implementations do not match every legacy feature. Template preservation, Office rendering, OCR, redlining, complex objects and formula calculation need separate implementations or authorized existing workflows. Do not substitute these CLIs for the platform's source-bound draft confirmation or publication path.

## Maintenance

Each package's MIT license applies only to its newly authored files. Preserve external dependency terms and generated notices for the actual distributed environment. Use public APIs and independent requirements for changes; do not port old restricted helpers into these directories.

Run the focused functional tests:

```bash
uv run --no-sync pytest packages/bowhead_skill/tests/test_community_document_skills.py
```

Evaluation prompts are included for subsequent model-based testing; the current automated gate verifies CLI behavior and document content rather than reporting an LLM comparison.

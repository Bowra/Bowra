# Authorship and dependency provenance

This package's instructions, schemas, scripts and synthetic examples were independently authored for Bowra on 2026-10-01, using the functional requirements below and public library APIs. The old restricted document Skill text, scripts, schemas, templates and assets were not used as implementation inputs. The old packages' license notices were inspected only to determine the existing redistribution constraint.

## Functional specification

`sheets` has 1–8 entries with unique `name` (at most 31 characters), `columns`, `rows`, and optional column `formats` (`general`, `integer`, `decimal`, `percent`). Cells are strings, finite numbers, booleans, null, or `{\"date\":\"YYYY-MM-DD\"}`. Every row/format list matches the column count. Limits: 2,000 data rows, 32 columns per sheet and 50,000 cells total.

Creation writes value-only worksheets, freezes header rows and adds filters. Strings beginning with `=` remain literal text. Explicit date objects become Excel dates. Inspection labels formula cells with `calculated: false`; it never substitutes stale cached values for a calculation. Charts, macros, external-link evaluation, formula creation and preserving existing templates are unsupported.

The maintained implementation is `scripts/run.py`; the executable input boundary is `input-schema.json`. No vendored Office schema, third-party helper implementation, font file, template or customer fixture is included. Library-provided default templates/fonts remain under their library's terms.

## Licenses

[LICENSE](LICENSE) grants MIT terms to this package's newly authored material. It does not change any other package's license. Dependencies are imported from the environment rather than copied into this package.

| Dependency | Tested version | Installed distribution license | Primary source                                                                        |
| ---------- | -------------- | ------------------------------ | ------------------------------------------------------------------------------------- |
| jsonschema | 4.26.0         | MIT                            | [Upstream license](https://github.com/python-jsonschema/jsonschema/blob/main/COPYING) |
| openpyxl   | 3.1.5          | MIT                            | [Upstream](https://openpyxl.readthedocs.io/en/stable/)                                |

Versions and classifications were checked against the installed distribution metadata; the installed dependency's license text remains authoritative. Keep dependency license/copyright files when distributing their wheels or runtime, and generate notices from the actual resolved closure, including transitive dependencies. This package's requirements pin tested direct versions; they are not a complete reproducible dependency lock.

[Public library documentation](https://openpyxl.readthedocs.io/en/stable/tutorial.html) informed API usage. No upstream implementation or documentation example was copied into the package. Future changes must retain this provenance record, exact dependency licenses and independently authored examples.

## Validation scope

The repository's `test_community_document_skills.py` exercises these real CLIs and reopens generated documents with the public libraries. [Evaluation prompts](evals/evals.json) describe representative authoring/inspection tasks. CLI assertions are deterministic functional checks, not an LLM quality benchmark or native Office fidelity certification.

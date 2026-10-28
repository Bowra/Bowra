# Authorship and dependency provenance

This package's instructions, schemas, scripts and synthetic examples were independently authored for Bowra on 2026-10-01, using the functional requirements below and public library APIs. The old restricted document Skill text, scripts, schemas, templates and assets were not used as implementation inputs. The old packages' license notices were inspected only to determine the existing redistribution constraint.

## Functional specification

Every block has one type: `heading` (`text`, `level` 1–6), `paragraph` (`text`), `bullets` (`items`), or `table` (`columns`, `rows`). Table cells are strings and each row has exactly the declared column count. Limits: 100 blocks, 4,000 total table cells; inspection: 2,000 blocks and 200,000 characters.

Creation supports a new document only. Inspection covers main-body paragraphs and top-level table text in order. Headers, footers, nested-table structure, tracked revisions, comments and drawings are outside this inventory; do not describe it as a complete document review. Use a separate authorized renderer for page previews. Template-preserving edits are not supported.

The maintained implementation is `scripts/run.py`; the executable input boundary is `input-schema.json`. No vendored Office schema, third-party helper implementation, font file, template or customer fixture is included. Library-provided default templates/fonts remain under their library's terms.

## Licenses

[LICENSE](LICENSE) grants MIT terms to this package's newly authored material. It does not change any other package's license. Dependencies are imported from the environment rather than copied into this package.

| Dependency  | Tested version | Installed distribution license | Primary source                                                                        |
| ----------- | -------------- | ------------------------------ | ------------------------------------------------------------------------------------- |
| jsonschema  | 4.26.0         | MIT                            | [Upstream license](https://github.com/python-jsonschema/jsonschema/blob/main/COPYING) |
| python-docx | 1.2.0          | MIT                            | [Upstream](https://github.com/python-openxml/python-docx/blob/master/LICENSE)         |

Versions and classifications were checked against the installed distribution metadata; the installed dependency's license text remains authoritative. Keep dependency license/copyright files when distributing their wheels or runtime, and generate notices from the actual resolved closure, including transitive dependencies. This package's requirements pin tested direct versions; they are not a complete reproducible dependency lock.

[Public library documentation](https://python-docx.readthedocs.io/en/latest/api/document.html) informed API usage. No upstream implementation or documentation example was copied into the package. Future changes must retain this provenance record, exact dependency licenses and independently authored examples.

## Validation scope

The repository's `test_community_document_skills.py` exercises these real CLIs and reopens generated documents with the public libraries. [Evaluation prompts](evals/evals.json) describe representative authoring/inspection tasks. CLI assertions are deterministic functional checks, not an LLM quality benchmark or native Office fidelity certification.

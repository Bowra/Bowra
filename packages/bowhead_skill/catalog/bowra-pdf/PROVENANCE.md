# Authorship and dependency provenance

This package's instructions, schemas, scripts and synthetic examples were independently authored for Bowra on 2026-10-01, using the functional requirements below and public library APIs. The old restricted document Skill text, scripts, schemas, templates and assets were not used as implementation inputs. The old packages' license notices were inspected only to determine the existing redistribution constraint.

## Functional specification

Blocks match DOCX: `heading`, `paragraph`, `bullets`, `table`. Optional `font_mode` is `latin` or `cjk`. Inspection is bounded to 100 pages and 200,000 extracted characters. PDF parsers can consume substantial resources on compressed streams; execute untrusted files only inside the caller's existing resource-controlled worker.

Creation lays out a new text/table report. Inspection extracts embedded text with page numbers and marks pages with no text; it cannot determine whether a page is blank or scanned. It does not inspect signatures, forms, annotations or visual layout. Encrypted inputs are rejected. OCR, form filling, merging and redaction are not implemented.

The maintained implementation is `scripts/run.py`; the executable input boundary is `input-schema.json`. No vendored Office schema, third-party helper implementation, font file, template or customer fixture is included. Library-provided default templates/fonts remain under their library's terms.

## Licenses

[LICENSE](LICENSE) grants MIT terms to this package's newly authored material. It does not change any other package's license. Dependencies are imported from the environment rather than copied into this package.

| Dependency | Tested version | Installed distribution license  | Primary source                                                                        |
| ---------- | -------------- | ------------------------------- | ------------------------------------------------------------------------------------- |
| jsonschema | 4.26.0         | MIT                             | [Upstream license](https://github.com/python-jsonschema/jsonschema/blob/main/COPYING) |
| pypdf      | 6.10.2         | BSD-3-Clause                    | [Upstream](https://github.com/py-pdf/pypdf/blob/main/LICENSE)                         |
| reportlab  | 4.4.10         | BSD (see installed license.txt) | [Upstream](https://docs.reportlab.com/)                                               |

Versions and classifications were checked against the installed distribution metadata; the installed dependency's license text remains authoritative. Keep dependency license/copyright files when distributing their wheels or runtime, and generate notices from the actual resolved closure, including transitive dependencies. This package's requirements pin tested direct versions; they are not a complete reproducible dependency lock.

[Public library documentation](https://pypdf.readthedocs.io/en/stable/user/extract-text.html) informed API usage. No upstream implementation or documentation example was copied into the package. Future changes must retain this provenance record, exact dependency licenses and independently authored examples.

## Validation scope

The repository's `test_community_document_skills.py` exercises these real CLIs and reopens generated documents with the public libraries. [Evaluation prompts](evals/evals.json) describe representative authoring/inspection tasks. CLI assertions are deterministic functional checks, not an LLM quality benchmark or native Office fidelity certification.

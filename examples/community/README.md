# Try Bowra with synthetic examples

English | [简体中文](README.zh-CN.md)

Start with a small task whose evidence you can inspect. These packs contain invented data, copyable prompts, and concrete checks for the answer.

| Example                                        | What to try                                           | What to check                                                                          |
| ---------------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------- |
| [Supplier review](supplier-review/README.md)   | Compare three proposals and write a decision memo     | Comparable costs, source references, missing evidence and a conditional recommendation |
| [Revenue review](revenue-review/README.md)     | Explain a quarter's growth and produce a report       | Reproducible metrics, segment drivers and limits on causal claims                      |
| [Agent automation](agent-automation/README.md) | Plan a scoped proposal → review → evaluation workflow | Exact version identities, limited grants and an owner-controlled release               |

## Start with the revenue pack

1. Open a chat in your configured Bowra installation.
2. Attach the pack's CSV and metric definitions, or paste their contents if your deployment does not accept those attachments.
3. Paste the [task prompt](revenue-review/task.md). Select an available Agent and resources suited to calculation/report writing.
4. Compare the result with [independently reproducible expectations](revenue-review/expected-results.md).

Configured model access, the selected Agent's resources, and your normal permissions still apply. A prompt cannot enable a Tool, grant file access or switch on an optional feature. For file generation, see [document Skill setup](../../docs/help/create-agent.en-US.md#choose-a-document-skill).

## Inspect real reference files

The revenue pack includes small [DOCX, PDF, XLSX and PPTX files](revenue-review/references/README.md) generated directly with the repository's independent `bowra-*` document CLIs. They illustrate supported output formats using checked synthetic numbers. They are **independently generated reference outputs**, not recorded application or LLM runs.

![Cropped first-page preview of the independently generated synthetic revenue PDF](revenue-review/references/report-preview.png)

## What these examples establish

- The inputs and expected calculations can be reviewed locally without an account or model key.
- Document reference sources and regeneration commands are included.
- Suggested tasks are starting points for your own configured installation; they are not claims of an executed product workflow or a passing CI pipeline.
- All suppliers, transactions, amounts and scenario details are synthetic. No customer records, private service addresses or credentials are included.

Follow [report revision help](../../docs/help/revise-report.en-US.md) when iterating on a real canvas report. A downloadable document, a canvas report, and a reviewed template draft are distinct deliverables; request the one you need.

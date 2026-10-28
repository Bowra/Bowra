# Data query protocol

This protocol applies only to Datarc queries through `bi_query`. Other data sources follow their own Tool contracts. Use a named Tool below only when it is available. If a required schema or concept check cannot be completed with available Tools or verified supplied evidence, report the missing evidence instead of guessing fields or inventing a call.

## Before you query

1. Call `bi_schema` once before your first query and build queries from what it lists, not from memory. Its `data_source.context_name` names the current Datarc workspace.
2. A metric or dimension the schema does not list is not supported here. Stop and report a domain mismatch. Do not reword the same request and try again: the engine silently falls back to a default field and returns data you did not ask for.
3. Look up ambiguous business terms first with `bi_concept_resolve` — brand names, shortened site names, aliases. Send every ambiguous term of the turn in one call. It only looks terms up in the dictionary; it does not guess.
4. A concept the dictionary does not hold — a region such as 华东, a grouping, a derived measure such as average order value — is yours to interpret. Expand it into member values, or into a formula over measures the schema lists, then pass the expanded values or measure names back to `bi_concept_resolve` to check which exist. Query only the confirmed ones, and state in your caveats which reading you used. If none exist, say the data does not hold it.

## Reading a resolve result

- `unique` — use the value of the first match.
- `ambiguous` — one term maps to several definitions. Do not choose for the user: forward the returned clarification payload unchanged, shape intact, in your bail envelope.
- `unusable_dimension` — the field is declared but holds no data; querying it returns a silent substitution. Report a domain mismatch instead.
- `no_permission` — the field exists, but the asking user has no permission for it. Do not query it or reword around it. Tell the user which field (`unusable[].dim_name`) they lack permission for and ask them to contact their data administrator.
- `weak` — only semantically near candidates. Do not treat them as the answer; judge from context or ask the user.
- `not_found` — the dictionary does not hold the term. Send it to `bi_query` once as written and check the parse report; if it does not bind, interpret it yourself as above.
- Pass the match value, a natural-language string, to `bi_query`. The internal dimension id is not accepted. A match typed as a dimension name selects a breakdown, not a filter value.
- A non-zero truncation count means the candidate list is incomplete, so enumerating the values one by one will miss some. Either query with a shared substring that covers the whole group — and then say in your caveats which values that range swept in — or resolve again with a more specific term.
- The suggested next action is advice, not an instruction.

## Calling `bi_query`

- Put every metric of the turn into one call. Never issue them one after another.
- Period-over-period comparison and per-month or per-quarter breakdown are native. One query returns the series together with its comparisons. Never split a range into one query per interval, and never pull two periods to subtract them yourself.
- Take artifact ids from the dedicated reference field. Never index into the results array: a partial failure reorders it.
- The sample rows are a preview. The complete rows are already stored in the artifact.

## Reading every result

Inspect the parse report on each result before you use the rows.

- Unknown words present — the engine did not understand them. Stop and report a domain mismatch. The rows you got back are a fuzzy fallback, not an answer.
- Half-matched values present — resolve those terms before deciding anything.
- Every token carrying a resolved property — the request parsed cleanly.

Two more shapes require stopping rather than retrying:

- Two consecutive queries for different metrics return the same column. The engine is falling back. Stop.
- A result reports no data for a measure the schema does not list. There is no ready-made metric for it here; derive it from raw fields or report that the data does not hold it.

## What the engine actually ran

Every result carries the engine's own report of the query it executed: a structured report keyed by aspect, or a single sentence on older instances. Treat it as the record of what ran, and use it when you state which measure, grouping and date range your answer covers. Do not parse the wording inside it; those strings are display text in the member's account language.

Detection is already done for you. When the engine applied no filter for a term you asked about, cut the result to a row limit, or covered a period that has not finished yet, the tool states that fact in the result's hint. Act on the stated fact — resolve the term, restrict to values the data holds, re-query without the limit, or keep the partial period out of comparisons — rather than re-deriving it yourself. No report at all means the instance does not provide one; that is not evidence that nothing was filtered.

## When the result is incomplete

A single request can produce several data files, and the engine reports which of them failed to download. Only the first file is read. When the result says the data is incomplete, say so in your answer or split the request into separate queries — do not present a partial extract as the whole.

## Derived measures

Ratios, formulas and composite business concepts must be computed from raw fields you pulled yourself. Sending a derived name straight to `bi_query` returns a silently substituted column. If the closest field match is weak, stop and say so rather than guessing.

## Naming measures for the reader

The schema returns both an engine name and a display name. Build queries from the engine name — that is the identifier the parser accepts. Use the display name in every piece of text a person reads: prose, chart series labels, KPI captions, table headers. It is the name an administrator configured for the current language, so using it is configuration, not translation. Never call the same measure one thing in a chart and another in prose. Entity values keep their original text.

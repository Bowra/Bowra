# Sandbox input and output protocol

This covers how data gets into and out of the isolated Python runtime. The analysis method is yours; these are the mechanics that fail silently when guessed.

## Declaring inputs

Pass data in by artifact id only, under a key **you choose**. The host writes each declared artifact to a file and hands the code a mapping from those same keys to file locations; reading the location out of that mapping is the only supported way to open it.

**The lookup key is the name you wrote in the declaration — nothing else.** It is not the artifact id, not a positional index, and not a variable you never assigned. If you declared an input under the name `rows`, the code reads it under `rows`. Looking it up by artifact id, by a made-up ordinal, or through an undefined variable raises before a single row is read.

Do not paste rows into the code string, do not treat a filename shown by the query tool as a relative path, do not invent a data directory, and do not wrap the path value in quotes as if it were a literal string.

A tabular input also carries its column names, dtypes, a small sample and a row count. **Print the column names before writing any dataframe code** — writing them from memory of the query you issued is the second most common real-world failure.

If a file is not found, the error text ends with the list of keys you actually declared and how to read them; follow that once and it will be right. An empty input directory means you declared nothing — declare it first, then read.

## Between calls

Every call starts a fresh container and a fresh working directory, so any path from a previous call is already gone. To reuse data you produced earlier, declare its output artifact id as an input again.

## Stating what this run is for

Every run states, in one sentence, what it is computing. That sentence is shown to the reader as the label of this data source, so write it for a person, not as code. When a turn runs the sandbox several times, give each run its own sentence — two identical labels read as a duplicated citation in the report.

## Deliverables versus working data

When the user asked for a downloadable file, say so explicitly and list the top-level filenames the code actually writes. Ordinary calculations, intermediate data and the data behind report charts are working data. Delivery intent cannot be guessed from a file extension or a MIME type.

## Giving up

If the same piece of code fails three times, change approach or stop and report the limitation. Repeated debugging of one cell burns the turn's budget.

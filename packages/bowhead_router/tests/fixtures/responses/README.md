# Responses protocol fixtures

These fixtures exercise provider-specific Responses JSON and SSE framing. The
sample tasks contain only counting, a weather tool call for Paris, and `pong`.
Request, message and tool-call IDs and timestamps are synthetic. The reasoning
text is independently authored test data with the original chunk lengths; it is
not a recorded model reasoning trace.

Retain the SSE line prefixes, whitespace variants, event ordering, delta lengths,
usage keys and optional/null fields when updating these files. They test supported
transport compatibility without requiring private documents or live credentials.

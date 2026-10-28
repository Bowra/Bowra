// Fixture bundling bypasses Next and has no `process`; modules reading `process.env.*` (api-client, etc.) require it to exist.
const scope = globalThis as unknown as { process?: { env: Record<string, string | undefined> } };
scope.process ??= { env: { NODE_ENV: "development" } };

export {};

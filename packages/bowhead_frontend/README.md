# Bowra frontend

English | [简体中文](README.zh-CN.md)

The Community interface uses Next.js, React, and TypeScript. Start with the repository's [build and setup guide](../../deploy/README.md) and [contributor guide](../../CONTRIBUTING.md).

## Development

Use Node.js 24 and npm. From the repository root:

```sh
make install-frontend
make run-frontend
```

Run the configured gateway in another terminal using `make run-gateway`. The development server proxies API requests to `http://localhost:8080`; browser session cookies stay on the frontend origin.

## Edition assembly

`build-manifest.json` selects the Community edition. `npm run dev`, `npm run build`, `npm run typecheck`, and `npm test` assemble the committed Community templates and message catalogs before running. Generated files are ignored and must not be edited; change `edition/community/` or `edition/shared-messages/` instead.

The Community build requires no Business source directory. Product names shown in the interface use **Bowra**. Existing `bowhead_*` identifiers and `BOWHEAD_*` configuration remain stable.

## Validation

```sh
make typecheck-frontend
make build-frontend
cd packages/bowhead_frontend
npm test
```

The static build writes `out/`. Public account and export pages use an independent layout that does not mount authenticated application components. Changes to chat rendering must preserve agreement between live events, persisted messages, and replayed conversations.

User Help articles live in `docs/help/` and are registered in `lib/help-content.ts`. Keep English and Chinese current rules synchronized, and preserve the other supported translations when changing visible behavior.

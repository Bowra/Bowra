# Security reporting

English | [简体中文](SECURITY.zh-CN.md)

## Report a vulnerability

Use **Security → Report a vulnerability** on this GitHub repository when private reporting is available. If that option is absent, open an issue requesting a secure contact method and include no vulnerability details, credentials, customer data or exploit material. Wait for the maintainer to establish a private channel before sharing sensitive information.

In the private report, describe the affected source revision, configuration, impact and minimum reproduction steps. Use synthetic accounts and data. Do not test another person's deployment without their authorization.

## Support scope

This repository is a Community source preview. It does not promise a response deadline, long-term support window or a hosted service. Check the repository's current release notes before relying on a specific build. Operators are responsible for updating dependencies and runtime images, protecting instance keys, restricting integrations and maintaining backups.

Report ordinary bugs without sensitive details through the repository's issue workflow. Preserve third-party security notices and attribution when proposing a fix.

## ChromaDB dependency advisories

As of 2026-11-09, the locked `chromadb` version is `1.5.9`. The Python package audit reports four distinct public advisories: [CVE-2026-45829](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c), [CVE-2026-45830](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr), [CVE-2026-45831](https://github.com/advisories/GHSA-xph7-9rjv-w5fr), and [CVE-2026-45833](https://github.com/advisories/GHSA-36p7-vc44-83pf). One advisory appears twice in the scanner output. No patched release is listed; see the [upstream release-status report](https://github.com/chroma-core/chroma/issues/7909). These findings remain open.

Bowra uses a local `PersistentClient` with application-supplied vectors and its own Team and Space access checks. Its application recipe does not start Chroma's standalone HTTP server or use Chroma's HTTP authorization provider. This limits the paths identified in the server advisories; it does not establish that the dependency is free of vulnerabilities. Chroma's client can also load embedding configuration, so do not connect this installation to an untrusted Chroma server, import untrusted Chroma storage/configuration, or enable remote model code. Keep the local Chroma directory writable only by the application and its trusted operator. Reassess these findings before changing that boundary or deploying a separate Chroma service.

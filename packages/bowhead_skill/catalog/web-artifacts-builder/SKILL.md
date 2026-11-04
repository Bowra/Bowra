---
name: web-artifacts-builder
description: "Build multi-component HTML artifacts (React + Tailwind + shadcn/ui). Use for complex artifacts needing state / routing. Not for simple single-file HTML."
license: Complete terms in LICENSE.txt
title: "Interactive web builder"
title_zh: "交互式网页构建"
category: design
kind: knowledge
---

# Web Artifacts Builder

Build HTML artifacts with the execution tools and preview capabilities actually available in the current environment:

1. Initialize the frontend repo using `scripts/init-artifact.sh`
2. Develop your artifact by editing the generated code
3. Bundle all code into a single HTML file using `scripts/bundle-artifact.sh`
4. Check the generated artifact and the interactions promised to the user
5. Provide the retained artifact and accurately state any unverified behavior

**Stack**: React 18 + TypeScript + Vite + Parcel (bundling) + Tailwind CSS + shadcn/ui

## Design & Style Guidelines

VERY IMPORTANT: To avoid what is often referred to as "AI slop", avoid using excessive centered layouts, purple gradients, uniform rounded corners, and Inter font.

## Quick Start

### Step 1: Initialize Project

Run the initialization script to create a new React project:

```bash
bash scripts/init-artifact.sh <project-name>
cd <project-name>
```

This creates a fully configured project with:

- ✅ React + TypeScript (via Vite)
- ✅ Tailwind CSS 3.4.1 with shadcn/ui theming system
- ✅ Path aliases (`@/`) configured
- ✅ 40+ shadcn/ui components pre-installed
- ✅ All Radix UI dependencies included
- ✅ Parcel configured for bundling (via .parcelrc)
- ✅ Node 18+ compatibility (auto-detects and pins Vite version)

### Step 2: Develop Your Artifact

To build the artifact, edit the generated files. See **Common Development Tasks** below for guidance.

### Step 3: Bundle to Single HTML File

To bundle the React app into a single HTML artifact:

```bash
bash scripts/bundle-artifact.sh
```

This creates `bundle.html`, with JavaScript, CSS and dependencies inlined. Use these build scripts only when the admitted execution environment actually provides Node and the required dependencies. Do not assume package installation or network access in the offline Python sandbox. A materialized HTML report with inline JavaScript can use the existing file-generation path without this frontend project setup.

Bowhead's authenticated HTML viewer can offer isolated offline Interactive mode when its capability is available. It blocks external connections, actual form submissions and cross-page navigation. Inline scripts must work within that boundary. Reading mode retains the static sanitizer. Do not assume every deployment or shared view can execute JavaScript.

For reports, prefer a materialized single-file HTML document that remains readable in Reading mode:

- all visible text, numbers, tables, and charts must already exist in the HTML;
- visible report content must not require a React mount, runtime fetch or external resource; add optional self-contained interactions only where supported;
- persist it with `text/html` or `application/xhtml+xml` MIME;
- then call `canvas_add_html_artifact` with the static artifact ID.

**Requirements**: Your project must have an `index.html` in the root directory.

**What the script does**:

- Installs bundling dependencies (parcel, @parcel/config-default, parcel-resolver-tspaths, html-inline)
- Creates `.parcelrc` config with path alias support
- Builds with Parcel (no source maps)
- Inlines all assets into single HTML using html-inline

### Step 4: Share Artifact with User

Present the retained HTML through the existing file-delivery path. Preserve the original when repairing an existing report; change only the requested markup or interaction and retain its business values.

### Step 5: Verify the promised behavior before claiming it works

Review native `html_static_check` diagnostics returned with file outputs or `present_artifacts`. They check duplicate IDs, fragment targets and resource attributes; they do not execute scripts or verify data. Correct reported structural problems before claiming the affected controls work. `not_checked` and `runtime_checked=false` are unverified states, not successful browser checks. Do not regenerate a retained file merely because an advisory inspection was unavailable.

Keep IDs unique across the document, including sections and their tables. Use distinct IDs such as `section-managers` and `table-managers`; bind each filter to the table and verify its element type before reading table rows. Contents links must address existing sections. Use `type="button"` for ordinary actions and prevent default submission for local calculations. Embed required assets instead of relying on CDNs.

When an authorized browser is available, test the user's requested contents navigation, both sort directions, filtering and clearing, and Full view entry/exit in the actual offline preview. Assert the visible destination, row order/coverage and retained state, not just absence of exceptions. Do not test actions that send data or modify external systems as if they were offline controls.

When no admitted browser is available, perform the available static checks, deliver readable content where possible and explicitly state that runtime interactions were not verified. Do not assume a development Playwright installation or the app's export renderer is an Agent Tool. Do not install a browser, rebuild a sandbox or relax preview isolation to bypass missing runtime capability.

## Reference

- **shadcn/ui components**: https://ui.shadcn.com/docs/components

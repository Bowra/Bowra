import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

/**
 * Vitest config — W12-14 adds jsdom + React plugin (JSX) + path aliases for component tests.
 *
 * - plugin: @vitejs/plugin-react handles .tsx JSX (not built into Vitest 4 rolldown)
 * - environment: jsdom
 * - resolve.alias "@/" → repository root (aligned with tsconfig.json paths)
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    // Node 25 enables its own Web Storage by default; that global localStorage shadows jsdom's
    // and has no methods without --localstorage-file. Tests must use the jsdom implementation.
    execArgv: ["--no-experimental-webstorage"],
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["**/node_modules/**", "**/e2e/**", "**/.next/**", "**/out-*/**", "**/dist/**"],
    globals: false,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
});

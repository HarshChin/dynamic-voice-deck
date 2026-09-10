import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Deliberately 127.0.0.1 and not "localhost".
//
// Node 17+ stopped reordering DNS results, so "localhost" resolves in whatever order the OS
// returns -- on macOS that is ::1 (IPv6) first. uvicorn binds IPv4, so a proxy aimed at
// "localhost:8000" reaches ::1, finds nothing there, and (worse) may reach some *other* process
// that happens to hold IPv6 port 8000, silently returning that process's HTML instead of our JSON.
// That is not hypothetical: it was observed on the development machine during the Phase 0 smoke
// test. Pinning the literal IPv4 address removes the ambiguity entirely.
const BACKEND_HTTP_ORIGIN = "http://127.0.0.1:8000";
const BACKEND_WS_ORIGIN = "ws://127.0.0.1:8000";

/**
 * Vite + Vitest configuration.
 *
 * `defineConfig` is imported from "vitest/config" (not "vite") so the `test` key is typed without a
 * triple-slash reference; "vitest/config" re-exports Vite's own `defineConfig` widened with Vitest's
 * options, which is the form that type-checks under Vite 8 / Vitest 4.
 *
 * The dev server proxies /api and /ws to the FastAPI backend so the app can use same-origin relative
 * URLs everywhere and never needs a base-URL environment variable in development.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: BACKEND_HTTP_ORIGIN,
        changeOrigin: true,
      },
      "/ws": {
        target: BACKEND_WS_ORIGIN,
        ws: true,
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test-setup.ts", "src/main.tsx"],
    },
  },
});

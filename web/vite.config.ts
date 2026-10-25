import { defineConfig } from "vite";

// During development the Mac service (or the mock, `npm run mock`) runs on :8000; /v1 is proxied
// to it so the site stays same-origin, exactly as when the service serves web/dist itself.
export default defineConfig({
  server: { host: true, proxy: { "/v1": "http://127.0.0.1:8000", "/health": "http://127.0.0.1:8000" } },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
  test: { include: ["test/**/*.test.ts"] },
} as any);

import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./desktop-ui", import.meta.url)),
    },
  },
  test: {
    // Both the renderer (jsdom) and main-process (node) suites run from one
    // vitest config. Main-process tests opt out of jsdom with a
    // `// @vitest-environment node` pragma at the top of the test file.
    include: ["desktop-ui/**/*.test.{ts,tsx}", "desktop-shell/**/*.test.ts"],
    environment: "jsdom",
    globals: false,
    setupFiles: ["./dev/vitest-setup.ts"],
  },
});

import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  define: {
    __SMARTRSS_PRECACHE_MANIFEST__: "[]",
    __SMARTRSS_BUILD_HASH__: '"test-build"',
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/test/unit/**/*.test.ts", "src/test/unit/**/*.test.tsx"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary", "html"],
      reportsDirectory: "./coverage",
      include: [
        "src/security/**/*.ts",
        "src/db/**/*.ts",
        "src/api/**/*.ts",
        "src/sync/**/*.ts",
        "src/search/**/*.ts",
      ],
      exclude: ["src/test/**"],
    },
  },
});

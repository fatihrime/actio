import { defineConfig } from "vitest/config";

export default defineConfig({
  // The "@/…" imports of tsconfig.json.
  resolve: { tsconfigPaths: true },
  test: {
    // Components are rendered into a simulated browser page (jsdom). The network is
    // never reached: each test says what `fetch` answers.
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    // A test that leaves a `fetch` stubbed, or a timer faked, doesn't hand it on.
    unstubGlobals: true,
    restoreMocks: true,
  },
});

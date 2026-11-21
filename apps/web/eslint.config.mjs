import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import tseslint from "typescript-eslint";

const SHIKI_STAYS_IN_THE_WORKER =
  "Shiki runs in the worker that colours code, and is imported by lib/chat/colouring.ts alone: on the page's thread it stops the page (ADR 0016).";

export default defineConfig([
  // Generated files: Next.js's own, the API's types (`make openapi`), and what the
  // end-to-end tests leave behind.
  globalIgnores([
    ".next/**",
    "next-env.d.ts",
    "lib/api/schema.d.ts",
    "playwright-report/**",
    "test-results/**",
  ]),
  js.configs.recommended,
  nextVitals,
  nextTs,
  // Type-aware rules, such as floating promises and unsafe `any`: the TypeScript
  // counterpart of mypy --strict on the Python side.
  tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  // Plain JavaScript config files are not part of the TypeScript project.
  {
    files: ["**/*.{js,mjs,cjs}"],
    extends: [tseslint.configs.disableTypeChecked],
  },
  // Code is coloured in a worker, by lib/chat/colouring.ts (ADR 0016). Shiki imported
  // anywhere else, at once or later, would colour on the page's own thread again,
  // which kept the page still for 1.5 s at a first block of TypeScript, and no test
  // would fail.
  {
    ignores: ["lib/chat/colouring.ts", "lib/chat/colouring.test.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["shiki", "shiki/*"],
              message: SHIKI_STAYS_IN_THE_WORKER,
            },
          ],
        },
      ],
      // The rule above doesn't look at `import(…)`.
      "no-restricted-syntax": [
        "error",
        {
          selector: "ImportExpression[source.value=/^shiki($|\\/)/]",
          message: SHIKI_STAYS_IN_THE_WORKER,
        },
      ],
    },
  },
]);

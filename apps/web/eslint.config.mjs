import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import tseslint from "typescript-eslint";

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
]);

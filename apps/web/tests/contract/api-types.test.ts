// The web app's types for the API are generated, and must be the ones the API's
// recorded description (apps/api/openapi.json) gives today.
//
// A Python test fails when the API no longer matches that file. This one fails when
// lib/api/schema.d.ts no longer matches it. After `make openapi` has written both
// again, the TypeScript compiler points at every call that no longer fits.
import { readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";
import { expect, it } from "vitest";

import { API_DESCRIPTION } from "../recorded";

const TYPES = path.join(import.meta.dirname, "../../lib/api/schema.d.ts");

it("lib/api/schema.d.ts is what the API's description generates", async () => {
  const description = await openapiTS(pathToFileURL(API_DESCRIPTION));
  const generated = COMMENT_HEADER + astToString(description);

  expect(
    generated === readFileSync(TYPES, "utf8"),
    "lib/api/schema.d.ts is out of date. Generate it again: make openapi",
  ).toBe(true);
});

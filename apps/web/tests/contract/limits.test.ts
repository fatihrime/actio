// A limit the page holds a person to must be the API's own. Were the page's smaller,
// it would refuse what the API takes; were it larger, the API would refuse what the
// page let through.
import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

import { TITLE_MAX } from "@/components/sidebar/row";

import { API_DESCRIPTION } from "../recorded";

interface Description {
  components: {
    schemas: Record<
      string,
      { properties: Record<string, { maxLength?: number }> }
    >;
  };
}

it("lets a title be as long as the API does", () => {
  const description = JSON.parse(
    readFileSync(API_DESCRIPTION, "utf8"),
  ) as Description;

  expect(TITLE_MAX).toBe(
    description.components.schemas.Rename?.properties.title?.maxLength,
  );
});

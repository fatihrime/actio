// A limit the page holds a person to must be the API's own. Were the page's smaller,
// it would refuse what the API takes; were it larger, the API would refuse what the
// page let through.
import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

import { TITLE_MAX } from "@/components/sidebar/row";
import { EMAIL_MAX, PASSWORD_MAX, PASSWORD_MIN } from "@/lib/account";

import { API_DESCRIPTION } from "../recorded";

interface Description {
  components: {
    schemas: Record<
      string,
      {
        properties: Record<string, { minLength?: number; maxLength?: number }>;
      }
    >;
  };
}

/** What the API's description says about a field of one of its request bodies. */
function field(
  schema: string,
  name: string,
): { minLength?: number; maxLength?: number } {
  const description = JSON.parse(
    readFileSync(API_DESCRIPTION, "utf8"),
  ) as Description;
  const found = description.components.schemas[schema]?.properties[name];
  expect(found, `${schema}.${name} in the API's description`).toBeDefined();
  return found ?? {};
}

it("lets a title be as long as the API does", () => {
  expect(TITLE_MAX).toBe(field("Rename", "title").maxLength);
});

it("asks of a new password what the API asks", () => {
  expect(field("SignUp", "password")).toMatchObject({
    minLength: PASSWORD_MIN,
    maxLength: PASSWORD_MAX,
  });
  // A changed password is held to the same.
  expect(field("PasswordChange", "new_password")).toMatchObject({
    minLength: PASSWORD_MIN,
    maxLength: PASSWORD_MAX,
  });
});

it("lets an email address be as long as the API does", () => {
  expect(EMAIL_MAX).toBe(field("SignUp", "email").maxLength);
  expect(EMAIL_MAX).toBe(field("LogIn", "email").maxLength);
});

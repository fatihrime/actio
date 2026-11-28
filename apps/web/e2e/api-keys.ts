// An account's API keys, made in its place on the page, for the specs that need a real
// key: the key the page shows once is read from the page, and never written in a spec.
import { randomUUID } from "node:crypto";

import type { Locator, Page } from "@playwright/test";

import { Chat, expect } from "./chat";

const PASSWORD = "correct horse battery";

/** A key's form (ADR 0033): `act_`, 8 digits and ASCII letters, `_`, and 43 more. */
const A_KEY = /^act_[0-9A-Za-z]{8}_[0-9A-Za-z]{43}$/;

/** An account of its own, signed up before any question is asked, in its place: its
 * address, and the section of its keys. */
export async function anAccountsKeys(
  chat: Chat,
  page: Page,
): Promise<{ email: string; keys: Locator }> {
  const email = `${randomUUID()}@example.com`;
  await chat.open();
  await expect(chat.box).toBeVisible();
  await chat.signUp(email, PASSWORD);
  await chat.accountLink(email).click();
  await expect(page).toHaveTitle("Your account · Actio");
  return { email, keys: page.getByRole("region", { name: "API keys" }) };
}

/** Make a key in the section, and hand back the whole of it, as the page shows it. */
export async function make(keys: Locator, name: string): Promise<string> {
  await keys.getByLabel("Name", { exact: true }).fill(name);
  await keys.getByRole("button", { name: "Make a key" }).click();
  const box = keys.getByRole("textbox", { name: `Your new key, “${name}”` });
  await expect(box).toHaveValue(A_KEY);
  return box.inputValue();
}

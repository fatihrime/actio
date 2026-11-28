// An account's API keys, made and revoked in its place on the page, through the whole
// stack (ADR 0033, revision "an account's keys in its place on the page"): the key the
// page shows once is the real one, the browser's clipboard takes it, and a key revoked
// there is refused. The stack's plan for accounts allows two keys
// (deploy/e2e/config/plans.toml). What the section says in each of its states is the
// component tests' (components/account/api-keys.test.tsx).
import type { APIRequestContext } from "@playwright/test";

import { anAccountsKeys, make } from "./api-keys";
import { expect, test } from "./chat";

/** What the API answers a request of the test's own, which carries `key` and no
 * cookie: nothing of the page's session. */
function askWith(request: APIRequestContext, key: string) {
  return request.get("/api/key", {
    headers: { Authorization: `Bearer ${key}` },
  });
}

test("a key made in the account's place is shown once, selected, copied, and is the one that works", async ({
  chat,
  page,
  context,
  request,
  baseURL,
}) => {
  // Headless Chromium refuses to write to the clipboard otherwise.
  await context.grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: new URL(baseURL ?? "").origin,
  });
  const { email, keys } = await anAccountsKeys(chat, page);
  await expect(keys.getByText(/An API key lets a program/)).toHaveText(
    "An API key lets a program use Actio as this account. Your plan allows 2 at once.",
  );

  const key = await make(keys, "Deploy script");

  // The keyboard on the box, with all of it selected: a copy by the keyboard works.
  const box = keys.getByRole("textbox", {
    name: "Your new key, “Deploy script”",
  });
  await expect(box).toBeFocused();
  expect(
    await box.evaluate((element: HTMLTextAreaElement) => [
      element.selectionStart,
      element.selectionEnd,
      document.activeElement === element,
    ]),
  ).toEqual([0, key.length, true]);

  await keys.getByRole("button", { name: "Copy" }).click();
  await expect(keys.getByText("Copied.")).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(key);

  // All of it in sight, whatever the window and the reader's letters: in a window
  // 320 px wide, with letters half again as large as the browser's own, the key takes
  // more rows than two, and the box is as high as they are. Its width says that the
  // page has heard of the window's: until then the list of conversations stands beside
  // the place, and leaves the box a few letters wide.
  const wide = page.viewportSize() ?? { width: 1280, height: 720 };
  await page.setViewportSize({ width: 320, height: 700 });
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "24px";
  });
  await expect
    .poll(() =>
      box.evaluate((element: HTMLTextAreaElement) => [
        element.clientWidth > 200,
        element.scrollWidth <= element.clientWidth,
        element.scrollHeight <= element.clientHeight,
      ]),
    )
    .toEqual([true, true, true]);
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "";
  });
  await page.setViewportSize(wide);

  const used = await askWith(request, key);
  expect(used.status()).toBe(200);
  expect(await used.json()).toMatchObject({
    name: "Deploy script",
    prefix: key.slice(0, 12),
    account: { email },
  });

  await keys.getByRole("button", { name: "Done" }).click();

  const listed = keys.getByRole("list", { name: "Your API keys" });
  await expect(listed.getByRole("listitem")).toHaveCount(1);
  await expect(listed).toContainText("Deploy script");
  await expect(listed).toContainText(`${key.slice(0, 12)}…`);
  expect(await page.content()).not.toContain(key);

  await page.reload();

  await expect(listed).toContainText("Deploy script");
  await expect(listed).toContainText(`${key.slice(0, 12)}…`);
  expect(await page.content()).not.toContain(key);
});

test("a second key reaches the plan's number, and one revoked through its question is refused", async ({
  chat,
  page,
  request,
}) => {
  const { keys } = await anAccountsKeys(chat, page);
  const first = await make(keys, "First");
  await keys.getByRole("button", { name: "Done" }).click();
  const second = await make(keys, "Second");
  await keys.getByRole("button", { name: "Done" }).click();

  // The sentence stands where the form was.
  await expect(
    keys.getByText(
      "You have as many keys as your plan allows. Revoke one to make another.",
    ),
  ).toBeVisible();
  await expect(keys.getByLabel("Name", { exact: true })).toHaveCount(0);
  const listed = keys.getByRole("list", { name: "Your API keys" });
  await expect(listed.getByRole("listitem")).toHaveCount(2);

  await keys.getByRole("button", { name: "Revoke “First”" }).click();
  const question = keys.getByRole("group", { name: "Revoke “First”?" });
  await expect(question.getByRole("button", { name: "Cancel" })).toBeFocused();
  await question.getByRole("button", { name: "Revoke", exact: true }).click();

  await expect(keys.getByLabel("Name", { exact: true })).toBeVisible();
  await expect(listed.getByRole("listitem")).toHaveCount(1);
  await expect(listed).toContainText("Second");
  expect((await askWith(request, first)).status()).toBe(401);
  expect((await askWith(request, second)).status()).toBe(200);
});

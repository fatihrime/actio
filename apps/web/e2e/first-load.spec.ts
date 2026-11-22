// A first visit asks for all of the page's scripts at once, while its stylesheet is still
// on its way (ADR 0015, revision of 2026-11-22). The one script the app writes into the
// page, which marks a list put away, stands at the start of the body. In the head, after
// the stylesheet, it held the browser's parser there until the stylesheet was in, and
// meanwhile Chromium asked for one of the page's scripts at a time. What only a real
// browser shows.
import type { Page } from "@playwright/test";

import { expect, test } from "./chat";
import { scriptsNamedIn } from "./scripts";

/** Hold back every script and stylesheet of the app's own, until the test lets them go
 * with `release`. Each is noted by its path as soon as it is asked for. */
async function holdTheAppsFiles(
  page: Page,
): Promise<{ asked: Set<string>; release: () => void }> {
  const asked = new Set<string>();
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/_next/static/**", async (route) => {
    const request = route.request();
    if (["script", "stylesheet"].includes(request.resourceType())) {
      asked.add(new URL(request.url()).pathname);
      await released;
    }
    await route.continue();
  });
  return { asked, release };
}

test("a first visit asks for all of the page's scripts while its stylesheet is still on its way", async ({
  chat,
  page,
}) => {
  const files = await holdTheAppsFiles(page);

  // As soon as the document is there: none of its files can be.
  const response = await page.goto("/", { waitUntil: "commit" });
  const named = scriptsNamedIn((await response?.text()) ?? "");
  // The control: the document names scripts of its own, more than one.
  expect(named.length).toBeGreaterThan(1);

  try {
    // Every one, while each of them and the stylesheet are held back. With the script
    // in the head, Chromium asked for one of them and waited, and this never held.
    await expect
      .poll(() => named.filter((path) => !files.asked.has(path)))
      .toEqual([]);
    // And the stylesheet was among what was held back.
    expect([...files.asked].some((path) => path.endsWith(".css"))).toBe(true);
  } finally {
    files.release();
  }

  await expect(chat.box).toBeEditable();
});

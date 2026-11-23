// The page asks for the model that answers to be loaded ahead of the first question to
// it (ADR 0026). What only the whole stack shows: the request's way through the proxy,
// with the session's cookie, past the API's check that a request which changes
// something comes from the API's own pages, with no body. The stack's model is on a
// server that has nothing to load (deploy/e2e/config/models.toml), so the API answers
// 204: no load is seen here. What the page makes of other answers, and when it asks,
// is for the component tests (components/chat/warm-up.test.tsx).
import type { Page, Response } from "@playwright/test";

import { expect, test } from "./chat";

/** The page's next request to have the model that answers loaded, once answered. */
function warmUp(page: Page): Promise<Response> {
  return page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/models/standard/warm-up",
  );
}

test("a visit asks for the model that answers to be loaded, and so does the page loaded again", async ({
  chat,
  page,
}) => {
  const first = warmUp(page);
  await chat.open();

  expect((await first).status()).toBe(204);
  await expect(chat.box).toBeEditable();

  const again = warmUp(page);
  await page.reload();

  expect((await again).status()).toBe(204);
  await expect(chat.box).toBeEditable();
});

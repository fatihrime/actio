// A model whose server can't answer is answered for by one that it names to stand in for
// it (ADR 0028), and the page says under the answer which model wrote it (revision "a
// turn says which model answered, and the page says so"). What only the whole stack
// shows: a real server that refuses the connection, passed over for the model that
// stands in, a turn's record that names the one that answered, the line under the box
// in that model's window, and the same after a reload. The rule's variants are the
// component tests' (lib/chat/figures.test.ts, lib/chat/context.test.ts,
// components/chat/answered-by.test.tsx).
import type { Page } from "@playwright/test";

import { expect, test } from "./chat";

/** The window of the model that stands in, the stack's default one
 * (deploy/e2e/config/models.toml), as the page words it. */
const WINDOW = "8,192";

/** What the API records of a turn, as far as this test reads it. */
interface Turn {
  model: string;
  answered_by: string | null;
  context_tokens: number | null;
}

/** The turn of the conversation's last answer, as the API says it: the conversation is
 * read with this page's session. */
async function lastTurn(page: Page): Promise<Turn> {
  await expect(page).toHaveURL(/\/c\//);
  const path = new URL(page.url()).pathname.replace(
    /^\/c\//,
    "/api/conversations/",
  );
  const response = await page.request.get(path);
  expect(response.status()).toBe(200);
  const stored = (await response.json()) as {
    messages: { metadata: { turn?: Turn | null } }[];
  };
  const turn = stored.messages.at(-1)?.metadata.turn;
  if (turn === undefined || turn === null) {
    throw new Error("the conversation ends with no answer");
  }
  return turn;
}

test("an answer written in the place of a model whose server isn't there says which model wrote it, and how full that one's window is, also after a reload", async ({
  chat,
  page,
}) => {
  // The stack's "away" is a model on a server that refuses every connection, which
  // names the fake under the default name to stand in for it. The page has no way to
  // choose it, so the first request for an answer is given it on its way out. The
  // requests after that go as the page made them.
  let toTheModelAway = true;
  await page.route("**/api/conversations/*/turns", async (route) => {
    const request = route.request();
    if (!toTheModelAway || request.method() !== "POST") {
      await route.continue();
      return;
    }
    toTheModelAway = false;
    const asked = JSON.parse(request.postData() ?? "{}") as Record<
      string,
      unknown
    >;
    await route.continue({
      postData: JSON.stringify({ ...asked, model: "away" }),
    });
  });
  const figures = page
    .getByRole("list", { name: "What this answer took" })
    .getByRole("listitem");
  const meter = page.getByRole("meter", { name: "Context" });
  await chat.open();

  await chat.ask("Name a bike part.");

  await expect(chat.answers).toHaveText("Echo: Name a bike part.");
  await chat.answered();
  // The turn is the model asked's, and its record names the one that wrote the answer.
  const turn = await lastTurn(page);
  expect([turn.model, turn.answered_by]).toEqual(["away", "standard"]);
  expect(turn.context_tokens).not.toBeNull();
  const tokens = new Intl.NumberFormat("en").format(turn.context_tokens ?? 0);
  // Last in the line under the answer, by its title in the models file.
  await expect(figures.last()).toHaveText("answered by Fake model");
  // The figure the model that answered counted, in its own window.
  await expect(meter).toHaveAttribute(
    "aria-valuetext",
    new RegExp(`^(under 1%|\\d+%) full: ${tokens} of ${WINDOW} tokens$`),
  );
  const took = await figures.allTextContents();
  const full = await meter.getAttribute("aria-valuetext");

  await page.reload();

  await expect(chat.answers).toHaveText("Echo: Name a bike part.");
  await expect(figures).toHaveText(took);
  await expect(meter).toHaveAttribute("aria-valuetext", full ?? "");
});

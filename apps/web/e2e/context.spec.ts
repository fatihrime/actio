// Under the box, one line says how full the model's context window is in this
// conversation (ADR 0015, revision "the page says how full the model's window is").
// What only the whole stack and a real browser show: the server's own figure behind the
// line, of the window the models file gives the model, the same words after a reload,
// and a line laid out by the real styles, which moves nothing when it comes and stays
// one line in a narrow window. The rule's variants are the component tests'
// (lib/chat/context.test.ts, components/chat/context-meter.test.tsx).
import type { Locator, Page } from "@playwright/test";

import { expect, test } from "./chat";
import type { Chat } from "./chat";

/** The window of the stack's model (deploy/e2e/config/models.toml), as the page words
 * it. */
const WINDOW = "8,192";

/** What the API records of a turn, as far as these tests read it. */
interface Turn {
  input_tokens: number | null;
  output_tokens: number | null;
  context_tokens: number | null;
}

/** The meter, as a screen reader finds it. */
function meterOf(page: Page): Locator {
  return page.getByRole("meter", { name: "Context" });
}

/** The meter's place: under the box, with a figure in it or none. */
function placeOf(chat: Chat): Locator {
  return chat.box.locator("xpath=ancestor::form[1]/following-sibling::*[1]");
}

/** The words beside the meter, as a person sees them: a screen reader is told the
 * meter's instead. */
function wordsOf(chat: Chat): Locator {
  return placeOf(chat).locator("[aria-hidden='true']");
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

/** A turn's figure, as English groups it. */
function figureOf(turn: Turn): string {
  if (turn.context_tokens === null) {
    throw new Error("the turn has no figure");
  }
  return new Intl.NumberFormat("en").format(turn.context_tokens);
}

test("after an answer the meter says its turn's figure, of the model's window, the same after a reload, and the box stays where it was", async ({
  chat,
  page,
}) => {
  await chat.open();
  await expect(page.getByText(/Answered by/)).toBeVisible();
  // The place is there before the figure, so the box is where it will stay.
  await expect(placeOf(chat)).toBeEmpty();
  const box = await chat.box.boundingBox();
  const log = await chat.scroller.boundingBox();

  await chat.ask("Name a bike part.");
  await expect(chat.answers).toHaveText("Echo: Name a bike part.");
  await chat.answered();

  const tokens = figureOf(await lastTurn(page));
  const said = new RegExp(
    `^(under 1%|\\d+%) full: ${tokens} of ${WINDOW} tokens$`,
  );
  await expect(meterOf(page)).toHaveAttribute("aria-valuetext", said);
  await expect(wordsOf(chat)).toHaveText(
    new RegExp(`^(under 1%|\\d+%) full · ${tokens} of ${WINDOW} tokens$`),
  );
  expect(await chat.box.boundingBox()).toEqual(box);
  expect(await chat.scroller.boundingBox()).toEqual(log);
  const words = await wordsOf(chat).textContent();

  await page.reload();

  await expect(chat.answers).toHaveText("Echo: Name a bike part.");
  await expect(meterOf(page)).toHaveAttribute("aria-valuetext", said);
  await expect(wordsOf(chat)).toHaveText(words ?? "");
});

test("after an answer that thought, the meter says the server's figure, less than what the model read and wrote", async ({
  chat,
  page,
}) => {
  await chat.open();
  await page.getByLabel("Think before answering").check();

  await chat.ask(
    "[[think: A saddle is what a rider sits on, and every bicycle has one, so it is a safe answer.]] Name a bike part.",
  );
  await expect(chat.answers).toContainText("Echo: Name a bike part.");
  await chat.answered();

  const turn = await lastTurn(page);
  // The thought isn't kept for the next question.
  expect(turn.input_tokens).not.toBeNull();
  expect(turn.output_tokens).not.toBeNull();
  expect(turn.context_tokens).toBeLessThan(
    (turn.input_tokens ?? 0) + (turn.output_tokens ?? 0),
  );
  await expect(meterOf(page)).toHaveAttribute(
    "aria-valuetext",
    new RegExp(` ${figureOf(turn)} of ${WINDOW} tokens$`),
  );
});

test("in a window 320 px wide the line is one line, as high as in a wide one, and says as much as it has room for: nothing in it overflows or is cut", async ({
  chat,
  page,
}) => {
  // A conversation that nearly fills the window, for the longest line there is. The
  // fake model counts a word a token: the instructions' 25 and the question's 5,500,
  // and an echo cut at the model's 2,048 (deploy/e2e/config/models.toml). About 92%.
  const question = Array.from({ length: 5_500 }, () => "spoke").join(" ");
  await page.setViewportSize({ width: 320, height: 640 });
  await chat.open();

  await chat.ask(question);
  // Thousands of words there and back: more than the few seconds a wait has by itself,
  // when the stack is busy with the other tests.
  await expect(chat.send).toBeVisible({ timeout: 20_000 });
  await chat.answered();
  await expect(meterOf(page)).toBeVisible();

  // The share alone: the tokens are left out where there is no room for them.
  await expect(wordsOf(chat)).toHaveText(/^9\d% full$/, {
    useInnerText: true,
  });
  const narrow = await placeOf(chat).boundingBox();
  // Neither the line nor its place is wider or higher than it shows, and the words
  // aren't cut short.
  const fits = () =>
    placeOf(chat).evaluate((place) =>
      [
        place,
        place.firstElementChild,
        place.querySelector("[aria-hidden='true']"),
      ].every(
        (part) =>
          part !== null &&
          part.scrollWidth <= part.clientWidth &&
          part.scrollHeight <= part.clientHeight,
      ),
    );
  expect(await fits()).toBe(true);

  // What the line says goes by the room it has itself. With the list beside the chat,
  // a window 768 px wide leaves it room for the tokens, and a wide window for all of it.
  for (const [width, said] of [
    [768, `^9\\d% full · [\\d,]+ of ${WINDOW} tokens$`],
    [
      1280,
      `^9\\d% full · [\\d,]+ of ${WINDOW} tokens · start a new chat soon$`,
    ],
  ] as const) {
    await page.setViewportSize({ width, height: 640 });

    await expect(chat.list).toBeVisible();
    await expect(wordsOf(chat)).toHaveText(new RegExp(said), {
      useInnerText: true,
    });
    expect((await placeOf(chat).boundingBox())?.height).toBe(narrow?.height);
    expect(await fits()).toBe(true);
  }
});

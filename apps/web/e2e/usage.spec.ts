// The usage limits, through the whole stack: the server counts what answers cost, the
// page shows the counts, a limit that is reached is said with when it resets, and a
// model that costs nothing carries on.
//
// On this stack a credit is one word of the fake model's, read or written, and an
// account may use 500 in a session (deploy/e2e/config/plans.toml). So a test that
// wants to reach the limit signs up first, and asks long questions. A guest's limit is
// out of reach, as every other test needs it to be.
import { randomUUID } from "node:crypto";

import { expect, test } from "./chat";
import type { Chat } from "./chat";

const PASSWORD = "correct horse battery";

/** A question of so many words. The fake model answers with the same words after
 * "Echo:", so a turn costs about twice the question, and what was said before it. */
function words(count: number): string {
  return Array.from({ length: count }, () => "word").join(" ");
}

/** The share of a limit that is used, in percent, as its meter says. */
async function used(chat: Chat, name: string): Promise<number> {
  return Number(await chat.meter(name).getAttribute("aria-valuenow"));
}

async function signUp(chat: Chat): Promise<void> {
  await chat.open();
  await chat.signUp(`${randomUUID()}@example.com`, PASSWORD);
  await expect(chat.usage).toContainText("Free plan");
}

test("a guest sees a meter that fills with what answers cost", async ({
  chat,
}) => {
  await chat.open();
  await expect(chat.usage).toContainText("Guest plan");
  await expect(chat.meter("Session")).toHaveAttribute(
    "aria-valuetext",
    "0% used",
  );
  await expect(chat.usage).toContainText("Starts with your next question");

  await chat.ask("Why is the sky blue?");
  await expect(chat.answers).toHaveText("Echo: Why is the sky blue?");

  // A few dozen credits of a guest's 100,000: counted, and far from a limit.
  await expect(chat.meter("Session")).toHaveAttribute(
    "aria-valuetext",
    "under 1% used",
  );
  await expect(chat.usage).toContainText(/Resets (at|on) /);
  await expect(chat.send).toBeVisible();
});

test("a limit that is reached is said, with when it resets, and a model that costs nothing carries on", async ({
  chat,
  page,
}) => {
  const reached = page.getByText(
    "You’ve reached your usage limit for this session.",
  );
  await signUp(chat);

  // Two questions fit: 106 credits, then 187, of the session's 500. The second takes
  // the session past half of what it allows, which the server says once, in that
  // answer's stream.
  await chat.ask(words(40));
  await expect(chat.answers).toHaveCount(1);
  await chat.answered();
  await expect.poll(() => used(chat, "Session")).toBeGreaterThan(10);
  await chat.ask(words(40));
  await expect(chat.answers).toHaveCount(2);
  await chat.answered();
  await expect(
    page.getByText(/You’ve used over 50% of your usage limit for this session/),
  ).toBeVisible();
  await expect.poll(() => used(chat, "Session")).toBeGreaterThan(50);
  const before = await used(chat, "Session");

  // The third doesn't: the server refuses it, and keeps nothing of it.
  await chat.ask(words(40));
  await expect(reached).toBeVisible();
  await expect(page.getByRole("timer")).toHaveText(
    /^in (5 hours|4 hours 5\d minutes)$/,
  );
  await expect(chat.questions).toHaveCount(2);
  await expect(chat.box).toHaveValue(words(40));
  await expect(chat.send).toBeDisabled();
  expect(await used(chat, "Session")).toBe(before);

  // The model that costs nothing takes the question that was refused.
  await page.getByRole("button", { name: "Use Local (free)" }).click();
  await expect(chat.box).toBeFocused();
  await expect(page.getByText("Local is answering, at no cost.")).toBeVisible();
  await chat.box.press("Enter");
  await expect(chat.answers).toHaveCount(3);
  await chat.answered();
  await expect(chat.answers.last()).toContainText("Echo: word word");
  expect(await used(chat, "Session")).toBe(before);

  // The counts are the server's: a page that is loaded again shows the same. That the
  // limit is reached, it learns as before, by asking.
  await page.reload();
  await expect.poll(() => used(chat, "Session")).toBe(before);
  await expect(chat.answers).toHaveCount(3);
  await expect(reached).toBeHidden();
  await chat.ask("And now?");
  await expect(reached).toBeVisible();
  await expect(chat.answers).toHaveCount(3);
});

test("an answer the limit leaves little room for is cut short, and says why", async ({
  chat,
  page,
}) => {
  await signUp(chat);

  // The question fits, with room for two thirds of its answer: 146 of its 221 words.
  // At 180 words the whole answer would fit, and at 290 the question alone would be
  // more than the plan allows (measured: ADR 0017, revision of 2026-11-15).
  await chat.ask(words(220));
  await chat.answered();

  await expect(
    page.getByText(
      "This answer stops here: your usage limit left no room for more of it.",
    ),
  ).toBeVisible();
  // One turn passed half of the limit and three quarters of it: the page says the
  // larger.
  await expect(
    page.getByText(/You’ve used over 75% of your usage limit for this session/),
  ).toBeVisible();
  await expect(page.getByText(/You’ve used over 50%/)).toHaveCount(0);
  expect(await used(chat, "Session")).toBeGreaterThanOrEqual(75);

  await page.reload();
  await expect(
    page.getByText(
      "This answer stops here: your usage limit left no room for more of it.",
    ),
  ).toBeVisible();
});

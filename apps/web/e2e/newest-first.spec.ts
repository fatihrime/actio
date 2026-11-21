// A long conversation comes onto the page newest first, at its end from the first frame
// that shows it, and its older messages are shown behind (ADR 0015, revision "a long
// conversation opens newest first"). What only a real browser shows: where the page
// is at each frame, while the older messages come in above, and that a reader who went
// up to them stays where they are. The order of the steps, what is drawn out of sight
// and when it is shown, and the rules of scrolling, are the component tests'
// (components/chat/newest-first.test.tsx, follow.test.tsx).
//
// The page is watched from inside, at the start of each frame, by a loop that begins
// before it shows the conversation. What the driver sees between two of its own calls
// is no frame. No test says how long a frame took: on a machine shared with other
// work that says little. Frame times are measured by hand, in three browsers. Nor is a
// wheel turned here: these tests run Chromium, which keeps a reader's own scroll while
// older messages come in, as Firefox and WebKit didn't; those are tried by hand.
import type { Page } from "@playwright/test";

import { expect, test } from "./chat";
import type { Chat } from "./chat";

// Making the conversation is most of each test: about 6 s for its 60 turns on a
// developer's machine, and CI is two to three times slower. Playwright's own limit of
// 30 s leaves too little room for that.
test.describe.configure({ timeout: 90_000 });

/** How many exchanges the conversation has: enough that its older messages come in
 * over several steps after the first frame. */
const EXCHANGES = 60;

/** Its messages: each exchange a question and its answer. */
const MESSAGES = 2 * EXCHANGES;

/** A question with a small table, which the fake model echoes: so each answer has a
 * little markdown. Few words, since the whole conversation is sent with each
 * question, and counted: there a guest may use 100,000 credits a session, at a credit
 * a word (deploy/e2e/config/plans.toml). A row of the table without spaces is one
 * word: the sixty exchanges used 21,630 credits, where rows with spaces between their
 * cells went past the limit at eighty. */
function questionOf(index: number): string {
  const number = String(index + 1);
  return [
    `Part ${number}:`,
    "",
    "|Part|Size|",
    "|-|-|",
    `|Spoke|${number}|`,
  ].join("\n");
}

/** Make a conversation of `EXCHANGES` exchanges, and hand back its id. It is made
 * through the API, from the page's own session, one turn after another, with what the
 * page posts for a question (lib/chat/turns.ts): typed into the box, sixty questions
 * would take far longer. */
async function aLongConversation(chat: Chat): Promise<string> {
  await chat.open();
  await expect(chat.box).toBeVisible(); // the page has its session
  const questions = Array.from({ length: EXCHANGES }, (_, index) =>
    questionOf(index),
  );
  return chat.page.evaluate(async (asked) => {
    const conversation = crypto.randomUUID();
    let parent: string | null = null;
    for (const text of asked) {
      const response: Response = await fetch(
        `/api/conversations/${conversation}/turns`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            message: {
              id: crypto.randomUUID(),
              parts: [{ type: "text", text }],
            },
            parent_id: parent,
            thinking: false,
          }),
        },
      );
      // Read to its end: the turn is over then. The answer's id is in its first
      // event, and the next question follows that answer.
      const stream: string = await response.text();
      const answer: string | undefined =
        /"type":"start","messageId":"([^"]+)"/.exec(stream)?.[1];
      if (!response.ok || answer === undefined) {
        throw new Error(
          `A turn was refused (${String(response.status)}): ${stream}`,
        );
      }
      parent = answer;
    }
    return conversation;
  }, questions);
}

/** What the page was like at the start of one frame. */
interface Frame {
  /** How many messages it showed: those drawn out of sight don't count. */
  messages: number;
  /** How far it was from the conversation's end, in pixels. */
  below: number;
  /** How far the message the reader went up to had moved since, in pixels: nothing
   * before they went up, and for a reader who doesn't. */
  moved: number | null;
}

/** What `watchFrames` saw. */
interface Seen {
  /** Each frame, from the first that showed a message. */
  frames: Frame[];
  /** How many messages the page showed when the reader went up: nothing before. */
  wentUpAt: number | null;
}

/** In the page: from now on, note at the start of each frame what the conversation is
 * like, from the first frame that shows a message of it. With `goUp`, the reader goes
 * to the top of what is shown once that frame has begun, in a task of its own, as a
 * hand would, and stays there. The top of what is shown is where a browser keeps no
 * reader's place by itself. */
function watchFrames(goUp: boolean): void {
  const seen: Seen = { frames: [], wentUpAt: null };
  Reflect.set(window, "framesSeen", seen);
  let goingUp = false;
  let watched: { message: Element; top: number } | null = null;
  // The messages the log shows, from the top: none inside what is out of sight.
  function shownIn(log: Element): Element[] {
    return [...log.querySelectorAll("article")].filter(
      (message) => message.closest("[hidden]") === null,
    );
  }
  function look() {
    const log = document.querySelector('[role="log"]');
    const scroller = log?.parentElement ?? null;
    const messages = log === null ? 0 : shownIn(log).length;
    if (log !== null && scroller !== null && messages > 0) {
      seen.frames.push({
        messages,
        below:
          scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight,
        moved:
          watched === null
            ? null
            : watched.message.getBoundingClientRect().top - watched.top,
      });
      if (goUp && !goingUp) {
        goingUp = true;
        setTimeout(() => {
          scroller.scrollTop = 0;
          const shown = shownIn(log);
          const first = shown[0];
          if (first !== undefined) {
            watched = {
              message: first,
              top: first.getBoundingClientRect().top,
            };
          }
          seen.wentUpAt = shown.length;
        }, 0);
      }
    }
    requestAnimationFrame(look);
  }
  requestAnimationFrame(look);
}

async function seen(page: Page): Promise<Seen> {
  return page.evaluate(() => Reflect.get(window, "framesSeen") as Seen);
}

/** Wait until every message of the conversation is on the page. */
async function allIn(chat: Chat): Promise<void> {
  await expect(chat.questions).toHaveCount(EXCHANGES);
  await expect(chat.answers).toHaveCount(EXCHANGES);
}

test("a long conversation opened from the list is at its end at every frame while it is filled in", async ({
  chat,
  page,
}) => {
  await aLongConversation(chat);
  await page.reload(); // the list is read again, with the conversation in it
  await expect(chat.listed).toHaveText(["Part 1:"]);
  await page.evaluate(watchFrames, false);

  await chat.listed.first().click();

  await allIn(chat);
  const { frames } = await seen(page);
  // The control: the first frame showed less than the whole, so these frames saw it
  // filled in, and not a page that was whole at once.
  expect(frames.length).toBeGreaterThan(0);
  expect(frames[0]?.messages).toBeLessThan(MESSAGES);
  // The page moves in the task that shows messages, before the next frame begins.
  expect(frames.filter((frame) => frame.below > 1)).toEqual([]);
  await expect(page.getByRole("log")).not.toHaveAttribute("aria-live");
  expect(await chat.below()).toBeLessThanOrEqual(1);
});

test("a long conversation opened by its address is at its end from its first frame", async ({
  chat,
  page,
}) => {
  const id = await aLongConversation(chat);
  await page.addInitScript(watchFrames, false);

  await chat.open(`/c/${id}`);

  await allIn(chat);
  const { frames } = await seen(page);
  expect(frames[0]?.messages).toBeLessThan(MESSAGES);
  expect(frames[0]?.below).toBeLessThanOrEqual(1);
});

test("a reader who goes up while a long conversation is filled in stays at what they read", async ({
  chat,
  page,
}) => {
  await aLongConversation(chat);
  await page.reload();
  await expect(chat.listed).toHaveText(["Part 1:"]);
  await page.evaluate(watchFrames, true);

  await chat.listed.first().click();

  await allIn(chat);
  await expect(
    page.getByRole("button", { name: "Go to the newest message" }),
  ).toBeVisible();
  const { frames, wentUpAt } = await seen(page);
  // The control: the reader went up while messages were still to come above.
  expect(wentUpAt).not.toBeNull();
  expect(wentUpAt).toBeLessThan(MESSAGES);
  const moved = frames.flatMap((frame) =>
    frame.moved === null ? [] : [frame.moved],
  );
  expect(moved.length).toBeGreaterThan(0);
  expect(moved.filter((by) => Math.abs(by) > 1)).toEqual([]);
});

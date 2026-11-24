// A long conversation comes onto the page newest first, at its end from the first frame
// that shows it, and its older messages are shown behind (ADR 0015, revision "a long
// conversation opens newest first"). What only a real browser shows: where the page
// is at each frame, while the older messages come in above, and that a reader who went
// up to them stays where they are. The order of the steps, what is drawn out of sight
// and when it is shown, and the rules of scrolling, are the component tests'
// (components/chat/newest-first.test.tsx, follow.test.tsx).
//
// A conversation its reader left away from its end opens where they left it, in the
// same tab, also once the page was loaded again: the message at the top of the screen
// where it stood, at every frame from the first, while the rest comes in above and
// below. Another tab opens it at its end. And the end of what is shown is not taken
// for the conversation's end while messages are still to come below it.
//
// The page is watched from inside, at the start of each frame, by a loop that begins
// before it shows the conversation. What the driver sees between two of its own calls
// is no frame. No test says how long a frame took: on a machine shared with other
// work that says little. Frame times are measured by hand, in three browsers. Nor is a
// wheel turned here: these tests run Chromium, which keeps a reader's own scroll while
// older messages come in, as Firefox and WebKit didn't; those are tried by hand.
import type { Locator, Page } from "@playwright/test";

import { Chat, expect, test } from "./chat";

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

/** A place in a conversation: the message at the top of the screen, the first whose
 * bottom edge is below the top edge of the part of the page that scrolls, and how far
 * that edge is below the message's top edge, in pixels. */
interface Place {
  message: string;
  offset: number;
}

/** The message the reader goes to: the answer to "Part 31", the 62nd of the 120. It
 * opens again with the 2 before it and the 7 after it, and messages to come above
 * and below. */
const PLACED = 61;

/** How far below its top the reader leaves the screen's top. */
const OFFSET = 40;

/** Make the conversation, open it from the list, go up to the `PLACED`th message as its
 * reader would, and leave for a new chat. Hands back the conversation's id, the place
 * as the page measures it, and the id of the conversation's last message. */
async function leftAtAPlace(
  chat: Chat,
): Promise<{ id: string; place: Place; last: string }> {
  const id = await aLongConversation(chat);
  await chat.page.reload();
  await expect(chat.listed).toHaveText(["Part 1:"]);
  await chat.listed.first().click();
  await allIn(chat);
  const left = await chat.scroller.evaluate(
    (scroller, { index, offset }) => {
      const messages = [
        ...scroller.querySelectorAll<HTMLElement>("[data-message-id]"),
      ];
      const top = () => scroller.getBoundingClientRect().top;
      const message = messages[index];
      if (message === undefined) {
        throw new Error("The message to go to isn't on the page.");
      }
      scroller.scrollTop +=
        message.getBoundingClientRect().top - top() + offset;
      return {
        place: {
          message: message.dataset.messageId ?? "",
          offset: top() - message.getBoundingClientRect().top,
        },
        last: messages.at(-1)?.dataset.messageId ?? "",
      };
    },
    { index: PLACED, offset: OFFSET },
  );
  // The page took it for the reader's scroll.
  await expect(newest(chat.page)).toBeVisible();
  await chat.newChat.click();
  await expect(chat.questions).toHaveCount(0);
  return { id, ...left };
}

/** The button that leads to the conversation's newest message. */
function newest(page: Page): Locator {
  return page.getByRole("button", { name: "Go to the newest message" });
}

/** What the page was like at the start of one frame, as `watchPlace` sees it. */
interface PlaceFrame {
  /** How many messages it showed. */
  messages: number;
  /** How far the top of the part that scrolls was below the top of the place's
   * message, in pixels: nothing while that message isn't shown. */
  offset: number | null;
  /** How far the message at the bottom of the screen had moved since the reader went
   * down to it, in pixels: nothing before. */
  moved: number | null;
}

/** What `watchPlace` saw. */
interface PlaceSeen {
  frames: PlaceFrame[];
  /** When the reader went down to the bottom of what was shown: whether the
   * conversation's last message was shown then. Nothing before. */
  wentDown: { lastShown: boolean } | null;
}

/** In the page: from now on, note at the start of each frame where the place's message
 * stands, from the first frame that shows a message, until this is called again.
 *
 * With `goDownTo`, the id of the conversation's last message, the reader goes to the
 * bottom of what is shown as soon as the first step below the place's messages is on
 * the page, and stays there. That is in the microtask after the task that put it
 * there: the later steps below can't have landed then, each being drawn in a task of
 * its own after it. And the page heard of the scroll in that same microtask, by an
 * event sent there and then: the browser's own comes only at the next frame, by which
 * time another step could have landed. */
function watchPlace({
  place,
  goDownTo,
}: {
  place: { message: string; offset: number };
  goDownTo: string | null;
}): void {
  const seen: PlaceSeen = { frames: [], wentDown: null };
  const watching = {};
  Reflect.set(window, "placeWatched", watching);
  Reflect.set(window, "placeSeen", seen);
  let watched: { message: Element; top: number } | null = null;
  function shownIn(log: Element): HTMLElement[] {
    return [...log.querySelectorAll<HTMLElement>("article")].filter(
      (message) => message.closest("[hidden]") === null,
    );
  }
  if (goDownTo !== null) {
    const steps = new MutationObserver((records) => {
      for (const record of records) {
        for (const added of record.addedNodes) {
          const log = added.parentElement;
          const scroller = log?.parentElement ?? null;
          // A step is a child of the log; the one below the place's messages comes
          // after the step that holds the place's message.
          const placed = log
            ?.querySelector(`[data-message-id="${place.message}"]`)
            ?.closest('[role="log"] > *');
          if (
            log?.getAttribute("role") !== "log" ||
            scroller === null ||
            !(added instanceof HTMLElement) ||
            placed === null ||
            placed === undefined ||
            !(
              placed.compareDocumentPosition(added) &
              Node.DOCUMENT_POSITION_FOLLOWING
            )
          ) {
            continue;
          }
          steps.disconnect();
          const now = shownIn(log);
          seen.wentDown = {
            lastShown: now.some((each) => each.dataset.messageId === goDownTo),
          };
          scroller.scrollTop = scroller.scrollHeight;
          scroller.dispatchEvent(new Event("scroll"));
          const bottom = now.at(-1);
          if (bottom !== undefined) {
            watched = {
              message: bottom,
              top: bottom.getBoundingClientRect().top,
            };
          }
          return;
        }
      }
    });
    steps.observe(document.body, { childList: true, subtree: true });
  }
  function look() {
    if (Reflect.get(window, "placeWatched") !== watching) {
      return;
    }
    const log = document.querySelector('[role="log"]');
    const scroller = log?.parentElement ?? null;
    const shown = log === null ? [] : shownIn(log);
    if (log !== null && scroller !== null && shown.length > 0) {
      const message = shown.find(
        (each) => each.dataset.messageId === place.message,
      );
      const top = scroller.getBoundingClientRect().top;
      seen.frames.push({
        messages: shown.length,
        offset:
          message === undefined
            ? null
            : top - message.getBoundingClientRect().top,
        moved:
          watched === null
            ? null
            : watched.message.getBoundingClientRect().top - watched.top,
      });
    }
    requestAnimationFrame(look);
  }
  requestAnimationFrame(look);
}

async function placeSeen(page: Page): Promise<PlaceSeen> {
  return page.evaluate(() => Reflect.get(window, "placeSeen") as PlaceSeen);
}

/** Expect the place's message where it stood at every frame `seen`, from the first that
 * showed the conversation, which showed less than the whole of it: so these frames saw
 * the rest come in. */
function expectAtThePlace(seen: PlaceSeen, place: Place): void {
  expect(seen.frames.length).toBeGreaterThan(0);
  expect(seen.frames[0]?.messages).toBeLessThan(MESSAGES);
  expect(
    seen.frames.filter(
      ({ offset }) => offset === null || Math.abs(offset - place.offset) > 2,
    ),
  ).toEqual([]);
}

test("a long conversation opened again from the list is where its reader left it, at every frame while it is filled in", async ({
  chat,
  page,
}) => {
  const { place } = await leftAtAPlace(chat);
  await page.evaluate(watchPlace, { place, goDownTo: null });

  await chat.listed.first().click();

  await allIn(chat);
  expectAtThePlace(await placeSeen(page), place);
  // The control: the place is the one the reader went to.
  expect(Math.abs(place.offset - OFFSET)).toBeLessThanOrEqual(1);
  await expect(newest(page)).toBeVisible();

  // The way to the newest message leads to the conversation's end, with every message
  // on the way, in order.
  await newest(page).click();

  await expect.poll(() => chat.below()).toBeLessThanOrEqual(1);
  await expect(chat.questions).toHaveText(
    Array.from(
      { length: EXCHANGES },
      (_, index) => new RegExp(`^Part ${String(index + 1)}:`),
    ),
  );
  await expect(chat.answers.last()).toContainText(`Part ${String(EXCHANGES)}:`);
  await expect(newest(page)).toBeHidden();
});

test("a long conversation opened again by Back, by its address, and loaded again in the same tab, is where its reader left it, at every frame while it is filled in; and in another tab at its end", async ({
  chat,
  page,
}) => {
  const { id, place } = await leftAtAPlace(chat);
  await page.evaluate(watchPlace, { place, goDownTo: null });

  await page.goBack();

  await allIn(chat);
  expectAtThePlace(await placeSeen(page), place);

  // Left again as it was, and opened by its address, without a page being loaded: the
  // address put into the page's history, and the page told, as Back tells it.
  await chat.newChat.click();
  await expect(chat.questions).toHaveCount(0);
  await page.evaluate(watchPlace, { place, goDownTo: null });

  await page.evaluate((path) => {
    window.history.pushState(null, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, `/c/${id}`);

  await allIn(chat);
  expectAtThePlace(await placeSeen(page), place);

  // The page keeps the places for the tab when it is left: loaded again, the
  // conversation is at the place from its first frame.
  await page.addInitScript(watchPlace, { place, goDownTo: null });

  await page.reload();

  await allIn(chat);
  expectAtThePlace(await placeSeen(page), place);

  // Another tab of the same browser, with the same session, has places of its own:
  // none there, so the conversation is at its end from its first frame.
  const other = await page.context().newPage();
  // The fixture watches the test's own page: this one's errors are the test's to see.
  const thrownThere: string[] = [];
  other.on("pageerror", (error) => thrownThere.push(error.message));
  await other.addInitScript(watchFrames, false);

  await other.goto(`/c/${id}`);

  await allIn(new Chat(other));
  const { frames } = await seen(other);
  expect(frames[0]?.messages).toBeLessThan(MESSAGES);
  expect(frames[0]?.below).toBeLessThanOrEqual(1);
  expect(thrownThere, "errors in the other tab's scripts").toEqual([]);
});

test("a reader who goes to the bottom of what is shown, while messages still come in below it, stays there", async ({
  chat,
  page,
}) => {
  const { place, last } = await leftAtAPlace(chat);
  await page.evaluate(watchPlace, { place, goDownTo: last });

  await chat.listed.first().click();

  await allIn(chat);
  const { frames, wentDown } = await placeSeen(page);
  // The control: the reader went down while the conversation's last message was still
  // to come below, as it has to be at that moment.
  expect(wentDown).toEqual({ lastShown: false });
  const moved = frames.flatMap((frame) =>
    frame.moved === null ? [] : [frame.moved],
  );
  expect(moved.length).toBeGreaterThan(0);
  expect(moved.filter((by) => Math.abs(by) > 1)).toEqual([]);
  // Not at the conversation's end: what came below is below them.
  expect(await chat.below()).toBeGreaterThan(48);
  await expect(newest(page)).toBeVisible();
});

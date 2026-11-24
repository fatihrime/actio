// Records the chat at work for the README's hero GIF, as Chromium paints it.
// `make demo-record` runs this against a running stack, then scripts/demo_gif.py makes
// the GIF of what it saved (docs/development.md, "The README's GIF").
//
// It drives the page as a person would, by the roles and names the end-to-end tests
// find things by (e2e/chat.ts), and records with Chromium's screencast: a frame
// whenever the picture changes, with the moment it was painted. Each frame is saved as
// a PNG file into the folder it is given, and `frames.json` there lists them in the
// order they came, as `{ name, at }`, `at` in seconds.
//
// Node runs it as it is, and strips the types itself:
//   node demo/record.mts <folder>
// from apps/web. ACTIO_DEMO_URL names the stack: http://localhost:8080 unless set.
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as pause } from "node:timers/promises";

import { chromium, errors } from "@playwright/test";
import type { Browser, Locator, Page } from "@playwright/test";

const STACK = process.env.ACTIO_DEMO_URL ?? "http://localhost:8080";
const QUESTION =
  "Explain a token bucket rate limiter in three short points, then show it in ten lines of Python.";
// Asked before the recording, so that the model is loaded by the time the recorded
// question reaches it.
const FIRST_QUESTION = "Say hello in three words.";

// How long each thing may take before the recording gives up, in seconds.
const PAGE_S = 30; // the page, its box, and every click and key
const BEGINS_S = 15; // an answer's beginning: the page shows Stop as soon as it asks
const ANSWER_S = 180; // an answer, the first one with the model's loading
const SAID_S = 10; // what the page says once an answer has ended
const TITLE_S = 8; // the title a model writes for the conversation, where one does
const DARK_S = 5; // the page turning dark

/** What kept the recording from being made, said in one line. */
class Missing extends Error {}

/** One saved frame: its file, and when Chromium painted it, in seconds. */
interface Frame {
  name: string;
  at: number;
}

/** What the page says while something keeps it waiting, such as a notice about the
 * model, or "Waiting for the model…". */
async function saying(page: Page): Promise<string> {
  const said = (await page.getByRole("status").allInnerTexts())
    .map((text) => text.replace(/\s+/g, " ").trim())
    .filter((text) => text !== "");
  return said.length === 0
    ? "The page says nothing about it."
    : `The page says: ${said.join(" / ")}`;
}

/** Wait until `what` is on the page, for `seconds` at the most, or fail with what
 * didn't come, and what the page says instead. */
async function until(
  page: Page,
  what: Locator,
  seconds: number,
  missing: string,
): Promise<void> {
  try {
    await what.waitFor({ timeout: seconds * 1000 });
  } catch (error) {
    if (error instanceof errors.TimeoutError) {
      throw new Missing(
        `${missing} within ${String(seconds)} s. ${await saying(page)}`,
      );
    }
    throw error;
  }
}

/** The chat's box to write in. */
function box(page: Page): Locator {
  return page.getByRole("textbox", { name: "Message" });
}

/** Open the chat, and wait for its box. */
async function open(page: Page): Promise<void> {
  let response;
  try {
    response = await page.goto("/");
  } catch {
    throw new Missing(
      `Nothing answers at ${STACK}. Start the stack with "make up", with a model that answers.`,
    );
  }
  if (response !== null && !response.ok()) {
    throw new Missing(
      `${STACK} answered ${String(response.status())} for the chat's page.`,
    );
  }
  await until(page, box(page), PAGE_S, "The chat's box did not appear");
}

/** Wait for the answer to the question just sent: until it has begun and ended, and
 * the page says what it took. */
async function answered(page: Page): Promise<void> {
  await until(
    page,
    page.getByRole("button", { name: "Stop" }),
    BEGINS_S,
    "No answer began",
  );
  await until(
    page,
    page.getByRole("button", { name: "Send" }),
    ANSWER_S,
    "The answer did not end",
  );
  await until(
    page,
    page.getByRole("list", { name: "What this answer took" }),
    SAID_S,
    "The page did not say what the answer took",
  );
}

/** Wait for the conversation's title, which a model writes once the first answer has
 * ended: until the conversation's name in the list is no longer its question's first
 * words. An installation need not have a model for titles, so when none comes the
 * recording goes on without one. */
async function titled(page: Page): Promise<void> {
  const named = page
    .getByRole("navigation", { name: "Conversations" })
    .getByRole("link")
    .first();
  const asked = QUESTION.slice(0, 12);
  for (let waited = 0; waited < TITLE_S * 1000; waited += 100) {
    const name = await named.innerText();
    if (!name.startsWith(asked)) {
      return;
    }
    await pause(100);
  }
}

/** Ask a short question in a browser context of its own, so that the model is
 * loaded, and the recorded answer starts at once. A guest's conversations are its
 * own: the recorded page's list stays empty. */
async function warmUp(browser: Browser): Promise<void> {
  const context = await browser.newContext({ baseURL: STACK });
  context.setDefaultTimeout(PAGE_S * 1000);
  try {
    const page = await context.newPage();
    await open(page);
    await box(page).fill(FIRST_QUESTION);
    await box(page).press("Enter");
    await answered(page);
  } finally {
    await context.close();
  }
}

/** Ask the question while Chromium records the page, and save what it painted into
 * `folder`. */
async function record(browser: Browser, folder: string): Promise<Frame[]> {
  const context = await browser.newContext({
    baseURL: STACK,
    viewport: { width: 960, height: 600 },
    deviceScaleFactor: 1,
    colorScheme: "light",
    // The page says times in the reader's way: in English, on whichever machine
    // records, and by that machine's own clock.
    locale: "en-US",
  });
  context.setDefaultTimeout(PAGE_S * 1000);
  try {
    const page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    const frames: Frame[] = [];
    let untimed = 0;
    cdp.on("Page.screencastFrame", ({ data, metadata, sessionId }) => {
      if (metadata.timestamp === undefined) {
        untimed += 1;
      } else {
        const name = `${String(frames.length).padStart(4, "0")}.png`;
        writeFileSync(join(folder, name), Buffer.from(data, "base64"));
        frames.push({ name, at: metadata.timestamp });
      }
      // Chromium sends the next frame once this one is acknowledged. That fails only
      // when the session is gone, and then the recording's next step fails too, and
      // says why.
      cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => undefined);
    });
    // Recorded from the chat as it stands ready: the page's first paints, blank and
    // then without its session, are not what the GIF begins with. What comes with the
    // session comes after the box: the model's name, the empty list, the usage. The
    // screencast begins with the picture Chromium painted last, so once those are on
    // the page, the page is given a moment to be painted.
    await open(page);
    await until(
      page,
      page.getByText("Answered by"),
      PAGE_S,
      "The page did not say which model answers",
    );
    await until(
      page,
      page.getByText("No conversations yet"),
      PAGE_S,
      "The list of conversations did not come",
    );
    await until(
      page,
      page.getByText("Session", { exact: true }),
      PAGE_S,
      "The usage did not come",
    );
    await pause(400);
    await cdp.send("Page.startScreencast", { format: "png", everyNthFrame: 1 });
    await pause(600);
    await box(page).pressSequentially(QUESTION, { delay: 28 });
    await pause(400);
    await box(page).press("Enter");
    await answered(page);
    await titled(page);
    await pause(1500);
    await page
      .getByRole("group", { name: "Theme" })
      .getByText("Dark", { exact: true })
      .click();
    await until(
      page,
      page.getByRole("radio", { name: "Dark", checked: true }),
      DARK_S,
      "The page did not turn dark",
    );
    await pause(2500);

    await cdp.send("Page.stopScreencast");
    if (untimed > 0) {
      throw new Missing(
        `Chromium sent ${String(untimed)} frames without the moment they were painted.`,
      );
    }
    writeFileSync(join(folder, "frames.json"), JSON.stringify(frames));
    return frames;
  } finally {
    await context.close();
  }
}

const folder = process.argv[2];
if (folder === undefined) {
  console.error("Usage: node demo/record.mts <folder for the frames>");
  process.exit(2);
}
mkdirSync(folder, { recursive: true });
const browser = await chromium.launch();
try {
  console.log(`Asking ${STACK} a first question, to have the model loaded…`);
  await warmUp(browser);
  console.log("Recording the chat…");
  const frames = await record(browser, folder);
  const first = frames[0]?.at ?? 0;
  const last = frames.at(-1)?.at ?? 0;
  console.log(
    `Recorded ${String(frames.length)} frames over ${(last - first).toFixed(1)} s.`,
  );
} catch (error) {
  console.error(error instanceof Missing ? error.message : error);
  process.exitCode = 1;
} finally {
  await browser.close();
}

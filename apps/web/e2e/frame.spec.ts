// The page's frame is there from the first paint. What arrives with the session fills
// places that were already there, so nothing on the page moves when it does (ADR 0015,
// revision of 2026-11-21). What only a real browser shows: the page painted from what
// the server sent before any of the app's scripts ran, laid out by the real styles,
// and the shifts the browser measures itself.
import type { Locator, Page } from "@playwright/test";

import { expect, test } from "./chat";
import type { Chat } from "./chat";

/** The window of every other test here, Playwright's for a desktop's Chrome: said
 * again, so that these stay wide whatever it becomes. */
const WIDE = { width: 1280, height: 720 };
const NARROW = { width: 390, height: 844 };

/** What a page may shift, in all, while it starts: a tenth of the most that counts as
 * good (0.1). */
const MOST_SHIFT = 0.01;

type Box = NonNullable<Awaited<ReturnType<Locator["boundingBox"]>>>;

/** Where the frame's parts are on the screen. */
interface Frame {
  header: Box;
  title: Box;
  main: Box;
}

/** Hold back the API's answer to the request that starts the session, until the test
 * lets it go with the function handed back. */
async function holdTheSession(page: Page): Promise<() => void> {
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/session/guest", async (route) => {
    await released;
    await route.continue();
  });
  return release;
}

/** From before the page's first script runs, and in every page loaded after, add up
 * the layout shifts the browser measures, leaving out those that follow a person's
 * input, as the browser's own score does. */
async function watchShifts(page: Page): Promise<void> {
  await page.addInitScript(() => {
    interface Shift {
      value: number;
      hadRecentInput: boolean;
    }
    let sum = 0;
    function add(entries: PerformanceEntryList) {
      for (const entry of entries as unknown as Shift[]) {
        if (!entry.hadRecentInput) {
          sum += entry.value;
        }
      }
    }
    const observer = new PerformanceObserver((list) => {
      add(list.getEntries());
    });
    observer.observe({ type: "layout-shift", buffered: true });
    Reflect.set(window, "shifted", () => {
      add(observer.takeRecords()); // measured, and not handed over yet
      return sum;
    });
  });
}

/** What `watchShifts` added up in the page that is open now, once the browser has
 * painted what is on it: a shift is measured when a frame is painted. */
async function shifted(page: Page): Promise<number> {
  return page.evaluate(async () => {
    await new Promise<void>((resolve) => {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          resolve();
        });
      });
    });
    return (Reflect.get(window, "shifted") as () => number)();
  });
}

async function boxOf(part: Locator): Promise<Box> {
  const box = await part.boundingBox();
  expect(box, "a part of the frame that has no box").not.toBeNull();
  return box ?? { x: 0, y: 0, width: 0, height: 0 };
}

async function frameOf(chat: Chat): Promise<Frame> {
  return {
    header: await boxOf(chat.header),
    title: await boxOf(chat.title),
    main: await boxOf(chat.main),
  };
}

/** Load a page with `load` while its session is held back, and take the frame while
 * the page says that it is starting. Then let the session come, wait until `shown` is
 * on the page, and take the frame again, with the sum of the layout shifts since the
 * page began. */
async function aroundTheSession(
  chat: Chat,
  load: () => Promise<unknown>,
  shown: Locator[],
): Promise<{ before: Frame; after: Frame; shift: number }> {
  const release = await holdTheSession(chat.page);
  await watchShifts(chat.page);
  await load();
  await expect(chat.starting).toBeVisible();
  const before = await frameOf(chat);

  release();
  // The last of what arrives with the session, beside what each test waits for.
  for (const part of [chat.logInLink, ...shown]) {
    await expect(part).toBeVisible();
  }

  const after = await frameOf(chat);
  return { before, after, shift: await shifted(chat.page) };
}

/** Ask a question, and wait until its conversation has its address. */
async function aConversation(chat: Chat, question: string): Promise<void> {
  await chat.open();
  await chat.ask(question);
  await expect(chat.answers).toHaveText(`Echo: ${question}`);
  await chat.answered();
  await expect(chat.page).toHaveURL(/\/c\//);
}

test.describe("in a wide window", () => {
  test.use({ viewport: WIDE });

  test("nothing moves when the session arrives at a new conversation", async ({
    chat,
    page,
  }) => {
    const { before, after, shift } = await aroundTheSession(
      chat,
      () => chat.open(),
      [
        chat.box,
        page.getByText(/Answered by/),
        chat.list.getByText(/No conversations yet/),
      ],
    );

    // The list's place stood beside the chat before the list was there.
    expect(before.main.x).toBeGreaterThan(0);
    expect(after).toEqual(before);
    expect(shift).toBeLessThan(MOST_SHIFT);
  });

  test("nothing moves when the session arrives at a stored conversation", async ({
    chat,
    page,
  }) => {
    await aConversation(chat, "Where does the frame stand?");

    const { before, after, shift } = await aroundTheSession(
      chat,
      () => page.reload(),
      [chat.answers, chat.listed.first()],
    );

    expect(before.main.x).toBeGreaterThan(0);
    expect(after).toEqual(before);
    expect(shift).toBeLessThan(MOST_SHIFT);
  });

  test("nothing moves when the session arrives for someone who put the list away", async ({
    chat,
    page,
  }) => {
    await chat.open();
    await chat.listButton.click();
    await expect(chat.listButton).toHaveAttribute("aria-expanded", "false");

    const { before, after, shift } = await aroundTheSession(
      chat,
      () => page.reload(),
      [chat.box, page.getByText(/Answered by/)],
    );

    // No place for the list, from the first paint: the chat has the whole width.
    expect(before.main.x).toBe(0);
    expect(after).toEqual(before);
    await expect(chat.list).toBeHidden();
    await expect(chat.listButton).toHaveAttribute("aria-expanded", "false");
    expect(shift).toBeLessThan(MOST_SHIFT);
  });

  test("the header's buttons can't be seen or used before the session is known", async ({
    chat,
    page,
  }) => {
    // Found as they are, seen or not: in a person's way, they are not.
    const hidden = [
      page.getByRole("button", {
        name: "Conversations",
        exact: true,
        includeHidden: true,
      }),
      page.getByRole("button", { name: "New chat", includeHidden: true }),
    ];
    const release = await holdTheSession(page);
    await chat.open();
    await expect(chat.starting).toBeVisible();

    for (const button of hidden) {
      await expect(button).toBeDisabled();
      await expect(button).toBeHidden();
    }
    await expect(chat.listButton).toHaveCount(0);
    await expect(chat.newChat).toHaveCount(0);

    release();

    for (const button of [chat.listButton, chat.newChat]) {
      await expect(button).toBeVisible();
      await expect(button).toBeEnabled();
    }
  });
});

test.describe("in a narrow window", () => {
  // A window that is only narrow, not a phone's profile: with touch emulated
  // (`isMobile`, `hasTouch`), Chromium marks each layout shift as one that followed a
  // person's input, and the sum leaves those out. It would say that nothing moved
  // where the title did. The boxes are compared as well.
  test.use({ viewport: NARROW });

  test("nothing moves when the session arrives at a new conversation", async ({
    chat,
    page,
  }) => {
    const { before, after, shift } = await aroundTheSession(
      chat,
      () => chat.open(),
      [chat.box, page.getByText(/Answered by/)],
    );

    expect(after).toEqual(before);
    expect(shift).toBeLessThan(MOST_SHIFT);
  });

  test("nothing moves when the session arrives at a stored conversation", async ({
    chat,
    page,
  }) => {
    await aConversation(chat, "Where does the frame stand?");

    const { before, after, shift } = await aroundTheSession(
      chat,
      () => page.reload(),
      [chat.answers],
    );

    expect(after).toEqual(before);
    expect(shift).toBeLessThan(MOST_SHIFT);
  });
});

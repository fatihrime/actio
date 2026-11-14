// What every end-to-end test starts from: the chat as a visitor finds it, and two
// things no test may let happen without saying so.
//
// The model behind the stack is the fake one (packages/fakellm). It answers
// "Echo: <the question>", and a question can script it: `[[pace: 0.1]]` makes it take
// its time, `[[think: ...]]` reason first, `[[fail: 503]]` fail. So a test says what
// the model does by typing.
import { expect, test as base } from "@playwright/test";
import type { Locator, Page } from "@playwright/test";

/** The chat page, as a person uses it. */
export class Chat {
  constructor(readonly page: Page) {}

  /** The box to write in. */
  get box(): Locator {
    return this.page.getByLabel("Message");
  }
  get send(): Locator {
    return this.page.getByRole("button", { name: "Send" });
  }
  get stop(): Locator {
    return this.page.getByRole("button", { name: "Stop" });
  }
  /** What was asked, oldest first. */
  get questions(): Locator {
    return this.page.getByRole("article", { name: "You" });
  }
  /** What the model answered, oldest first. */
  get answers(): Locator {
    return this.page.getByRole("article", { name: "Actio" });
  }
  /** The part of the page that scrolls: the conversation, without the box below. */
  get scroller(): Locator {
    return this.page.getByRole("log").locator("xpath=..");
  }
  /** The list of conversations beside the chat. */
  get list(): Locator {
    return this.page.getByRole("navigation", { name: "Conversations" });
  }
  /** The conversations in the list, from the top. */
  get listed(): Locator {
    return this.list.getByRole("link");
  }
  /** The button that shows the list, and puts it away. */
  get listButton(): Locator {
    return this.page.getByRole("button", {
      name: "Conversations",
      exact: true,
    });
  }
  get newChat(): Locator {
    return this.page.getByRole("button", { name: "New chat" });
  }

  async open(path = "/"): Promise<void> {
    await this.page.goto(path);
  }

  /** Type a question and send it. Does not wait for the answer. */
  async ask(question: string): Promise<void> {
    await this.box.fill(question);
    await this.box.press("Enter");
  }

  /** Wait until the answer that was being written has ended, however it ended. */
  async answered(): Promise<void> {
    await expect(this.send).toBeVisible();
    await expect(this.stop).toBeHidden();
  }

  /** Ask a question in a new conversation, and wait for its answer. */
  async converse(question: string): Promise<void> {
    await this.newChat.click();
    await expect(this.questions).toHaveCount(0);
    await this.ask(question);
    await expect(this.answers).toHaveText(`Echo: ${question}`);
    await this.answered();
  }

  /** How far the reader is from the end of the conversation, in pixels. */
  async below(): Promise<number> {
    return this.scroller.evaluate((element) =>
      Math.round(
        element.scrollHeight - element.scrollTop - element.clientHeight,
      ),
    );
  }

  /** How much of the conversation doesn't fit on the screen, in pixels. */
  async hidden(): Promise<number> {
    return this.scroller.evaluate(
      (element) => element.scrollHeight - element.clientHeight,
    );
  }
}

/** What the page asked of other sites. Every such request is refused and noted, and
 * a test ends with a failure unless `expected` names exactly what was asked. */
export interface Outside {
  asked: string[];
  expected: string[];
}

export const test = base.extend<{
  chat: Chat;
  outside: Outside;
  unbroken: undefined;
}>({
  // Playwright calls the second argument `use`. It has another name here, because the
  // linter takes a call to `use` for React's hook of that name.
  chat: async ({ page }, give) => {
    await give(new Chat(page));
  },

  // Nothing leaves the stack, in any test. An answer is a model's text, and it must
  // not be able to make the browser fetch (ADR 0016): a test that shows one proves so
  // by ending here with nothing asked.
  outside: [
    async ({ context, baseURL }, give) => {
      const here = new URL(baseURL ?? "").origin;
      const outside: Outside = { asked: [], expected: [] };
      await context.route(
        (url) => url.origin !== here,
        async (route) => {
          outside.asked.push(route.request().url());
          await route.abort();
        },
      );
      await give(outside);
      expect(outside.asked, "what the page asked of other sites").toEqual(
        outside.expected,
      );
    },
    { auto: true },
  ],

  // No test passes on a page whose scripts broke.
  unbroken: [
    async ({ page }, give) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await give(undefined);
      expect(errors, "errors in the page's scripts").toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };

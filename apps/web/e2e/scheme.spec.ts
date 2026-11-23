// Light or dark: as the reader chose, or as their system is set (ADR 0015, revision "the
// reader chooses light or dark"). What only a real browser shows: what CSS makes of the
// choice, from before any of the app's scripts have run, and a diagram that a script
// draws in the scheme's colours. The system's setting is Playwright's (`colorScheme`,
// `page.emulateMedia`).
//
// Colours are read from the page as the browser computes them, and compared with what
// the same page shows in the other scheme, or with a token's own two colours: no
// colour is written into a test.
import type { Locator, Page } from "@playwright/test";

import { expect, test } from "./chat";
import type { Chat } from "./chat";
import { isAppScript } from "./scripts";

type Setting = "light" | "dark";

/** A question whose echo holds a block of TypeScript and a diagram. */
const CODE_AND_A_DIAGRAM = [
  "this:",
  "",
  "```ts",
  "const wheels = 2;",
  "```",
  "",
  "```mermaid",
  "flowchart TD",
  "  A[Someone asks] --> B[The model answers]",
  "```",
].join("\n");

/** How long to wait for code in colours, or a diagram drawn: the first of each fetches
 * a large library, while the rest of the suite runs beside it. */
const DRAWN = { timeout: 20_000 };

/** The keyword in the block of code, once it is coloured: code without colours is one
 * piece of text a line. */
function keyword(chat: Chat): Locator {
  return chat.answers
    .locator('[data-streamdown="code-block"] pre span', { hasText: /^const$/ })
    .first();
}

/** The shape of the diagram's first node. */
function node(chat: Chat): Locator {
  return chat.answers
    .locator('[data-streamdown="mermaid"] [role=img] svg .node')
    .first()
    .locator("rect, polygon, path")
    .first();
}

/** The list of conversations, or its place while nobody has a session: what stands
 * before the chat, in a wide window. */
function listPlace(chat: Chat): Locator {
  return chat.main.locator("xpath=preceding-sibling::*[1]");
}

/** What the page's frame looks like: there from the first paint. */
interface Frame {
  /** The colour of the words: the product's name at the top. */
  words: string;
  /** The ground of the list of conversations. */
  list: string;
}

/** What the page looks like, with an answer that holds code and a diagram. */
interface Looks extends Frame {
  /** The keyword's colour, and the two its token carries: the light theme's and the
   * dark theme's. */
  keyword: { shown: string; light: string; dark: string };
  /** The fill of the diagram's first node. */
  diagram: string;
}

async function frameOf(chat: Chat): Promise<Frame> {
  return {
    words: await chat.title.evaluate((title) => getComputedStyle(title).color),
    list: await listPlace(chat).evaluate(
      (place) => getComputedStyle(place).backgroundColor,
    ),
  };
}

async function looksOf(chat: Chat): Promise<Looks> {
  return {
    ...(await frameOf(chat)),
    keyword: await keyword(chat).evaluate((token: HTMLElement) => {
      // A colour as the browser computes it: as a probe of that colour is shown.
      const computed = (colour: string) => {
        const probe = document.createElement("span");
        probe.style.color = colour;
        document.body.append(probe);
        const shown = getComputedStyle(probe).color;
        probe.remove();
        return shown;
      };
      return {
        shown: getComputedStyle(token).color,
        light: computed(token.style.getPropertyValue("--sdm-c")),
        dark: computed(token.style.getPropertyValue("--shiki-dark")),
      };
    }),
    diagram: await node(chat).evaluate((shape) => getComputedStyle(shape).fill),
  };
}

/** Ask for an answer with code and a diagram, and wait until the code is coloured and
 * the diagram drawn. */
async function anAnswerWithCodeAndADiagram(chat: Chat): Promise<void> {
  await chat.open();
  await chat.ask(CODE_AND_A_DIAGRAM);
  await expect(keyword(chat)).toHaveAttribute("style", /--shiki-dark/, DRAWN);
  await expect(node(chat)).toBeVisible(DRAWN);
  await chat.answered();
}

/** What the page looks like once its code is in `setting`'s colours and its diagram is
 * drawn: drawn anew, where it was drawn as `before` until now. */
async function settledIn(
  chat: Chat,
  setting: Setting,
  before?: string,
): Promise<Looks> {
  let seen: Looks | undefined;
  await expect
    .poll(async () => {
      seen = await looksOf(chat);
      return seen.keyword.shown === seen.keyword[setting] &&
        seen.diagram !== before
        ? "settled"
        : seen;
    }, DRAWN)
    .toBe("settled");
  if (seen === undefined) {
    throw new Error("The page was never looked at.");
  }
  return seen;
}

/** What the page looks like with nothing chosen, as the system is set: `first`, then
 * the other, the page open all along, then `first` again. */
async function asTheSystemIsSet(
  chat: Chat,
  first: Setting,
): Promise<Record<Setting, Looks>> {
  const other = first === "light" ? "dark" : "light";
  await chat.page.emulateMedia({ colorScheme: first });
  const here = await settledIn(chat, first);
  await chat.page.emulateMedia({ colorScheme: other });
  const there = await settledIn(chat, other, here.diagram);
  await chat.page.emulateMedia({ colorScheme: first });
  await expect.poll(() => looksOf(chat), DRAWN).toEqual(here);
  return first === "light"
    ? { light: here, dark: there }
    : { light: there, dark: here };
}

/** Hold back every script of the app's own, until the test lets them go with
 * `release`. `held` says how many were asked for meanwhile. */
async function holdTheAppsScripts(
  page: Page,
): Promise<{ held: () => number; release: () => void }> {
  let held = 0;
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/_next/static/**", async (route) => {
    if (isAppScript(route.request())) {
      held += 1;
      await released;
    }
    await route.continue();
  });
  return { held: () => held, release };
}

test.describe("with nothing chosen", () => {
  test.use({ colorScheme: "light" });

  test("the page follows the system, light and dark, also when it changes while the page is open", async ({
    chat,
    page,
  }) => {
    test.slow();
    await anAnswerWithCodeAndADiagram(chat);

    const system = await asTheSystemIsSet(chat, "light");

    // The control for the tests below: every part differs between the two.
    expect(system.dark.words).not.toBe(system.light.words);
    expect(system.dark.list).not.toBe(system.light.list);
    expect(system.dark.diagram).not.toBe(system.light.diagram);
    expect(system.light.keyword.light).not.toBe(system.light.keyword.dark);
    // And the code is in its own theme's colours in each.
    expect(system.light.keyword.shown).toBe(system.light.keyword.light);
    expect(system.dark.keyword.shown).toBe(system.dark.keyword.dark);
    await expect(page.locator("html")).not.toHaveAttribute("data-scheme");
    await expect(
      chat.theme.getByRole("radio", { name: "System" }),
    ).toBeChecked();
  });
});

test.describe("under a light system", () => {
  test.use({ colorScheme: "light" });

  test("Dark chosen is dark, from before any of the app's scripts have run after a reload", async ({
    chat,
    page,
  }) => {
    test.slow();
    await anAnswerWithCodeAndADiagram(chat);
    const system = await asTheSystemIsSet(chat, "light");

    await chat.chooseTheme("Dark");

    await expect.poll(() => looksOf(chat), DRAWN).toEqual(system.dark);
    await expect(page.locator("html")).toHaveAttribute("data-scheme", "dark");

    // Loaded again, with every one of the app's scripts held back: the script at the
    // start of the body marked the page, and the styles show it.
    const scripts = await holdTheAppsScripts(page);
    try {
      await page.reload({ waitUntil: "commit" });
      await expect(chat.starting).toBeVisible();
      await expect.poll(scripts.held).toBeGreaterThan(0);

      await expect(page.locator("html")).toHaveAttribute("data-scheme", "dark");
      expect(await frameOf(chat)).toEqual({
        words: system.dark.words,
        list: system.dark.list,
      });
    } finally {
      scripts.release();
    }

    await expect(chat.theme.getByRole("radio", { name: "Dark" })).toBeChecked();
    await expect.poll(() => looksOf(chat), DRAWN).toEqual(system.dark);
  });
});

test.describe("on a page that React can't take over as it was sent", () => {
  test.use({ colorScheme: "light" });

  test("Dark chosen is dark at every frame, and a list put away stays away, while the page is drawn anew", async ({
    chat,
    page,
    thrown,
  }) => {
    await chat.open();
    await expect(chat.theme).toBeVisible();
    const light = await frameOf(chat);
    await page.emulateMedia({ colorScheme: "dark" });
    await expect.poll(() => frameOf(chat)).not.toEqual(light);
    const dark = await frameOf(chat);
    await page.emulateMedia({ colorScheme: "light" });
    await chat.chooseTheme("Dark");
    await expect.poll(() => frameOf(chat)).toEqual(dark);
    await chat.listButton.click();
    await expect(chat.listButton).toHaveAttribute("aria-expanded", "false");

    // In the page that is loaded next, from before its first script: what each frame
    // finds of the two marks on the page's root, whenever that differs from the frame
    // before.
    await page.addInitScript(() => {
      const found: string[] = [];
      Reflect.set(window, "marksFound", found);
      const look = () => {
        const root = document.documentElement;
        const marks = `${root.getAttribute("data-scheme") ?? "no scheme"}, ${
          root.hasAttribute("data-list-away") ? "list away" : "list shown"
        }`;
        if (found.at(-1) !== marks) {
          found.push(marks);
        }
        requestAnimationFrame(look);
      };
      requestAnimationFrame(look);
    });

    // The server's page with one word changed on its way, as an extension of the
    // browser's may change it. React finds that the page isn't what it would draw,
    // gives up taking it over, which is its error 418, and draws the page anew: that
    // takes every attribute off the page's root, the script's marks with them.
    thrown.expect(/Minified React error #418/);
    await page.route(
      (url) => url.pathname === "/",
      async (route) => {
        if (route.request().resourceType() !== "document") {
          await route.continue();
          return;
        }
        const response = await route.fetch();
        const sent = await response.text();
        expect(sent).toContain(">Actio</h1>");
        await route.fulfill({
          response,
          body: sent.replace(">Actio</h1>", ">Actio, changed</h1>"),
        });
      },
    );
    await page.reload();

    // The page was drawn anew: the word is the page's own again, and React said so.
    await expect(chat.title).toHaveText("Actio");
    await expect.poll(() => thrown.errors).not.toEqual([]);
    await expect(chat.listButton).toHaveAttribute("aria-expanded", "false");
    await expect(page.locator("html")).toHaveAttribute("data-scheme", "dark");
    await expect(page.locator("html")).toHaveAttribute("data-list-away", "");
    await expect(chat.list).toBeHidden();

    // And no frame found the page without its marks, once the script had set them:
    // the page puts them back before the browser paints. Put back after the paint, a
    // frame showed the system's scheme, and the list's place.
    const found = await page.evaluate(
      () => Reflect.get(window, "marksFound") as string[],
    );
    expect(found).toContain("dark, list away");
    expect(found.slice(found.indexOf("dark, list away"))).toEqual([
      "dark, list away",
    ]);

    await chat.listButton.click();
    await expect(chat.theme.getByRole("radio", { name: "Dark" })).toBeChecked();
    expect(await frameOf(chat)).toEqual(dark);
  });
});

test("the outline around a choice is drawn at the group's own word that an arrow key was used", async ({
  chat,
}) => {
  // WebKit doesn't say that the keyboard is in use of a radio button an arrow key moved
  // to, so the group notes the key itself, and the styles go by that note as well
  // (components/sidebar/theme.tsx). This browser does say so. So the note is put on the
  // group by hand here, after a click, at which no browser says that the keyboard is
  // in use: what is drawn then, the styles drew for the note alone.
  const outlines = () =>
    chat.theme
      .locator("label")
      .evaluateAll((labels) =>
        labels.map(
          (label) =>
            `${label.textContent}: ${getComputedStyle(label).outlineStyle}`,
        ),
      );
  await chat.open();
  await chat.chooseTheme("Dark");
  await expect(chat.theme.getByRole("radio", { name: "Dark" })).toBeFocused();
  expect(await outlines()).toEqual([
    "System: none",
    "Light: none",
    "Dark: none",
  ]);

  await chat.theme.evaluate((group) => {
    group.setAttribute("data-keys", "");
  });

  expect(await outlines()).toEqual([
    "System: none",
    "Light: none",
    "Dark: solid",
  ]);
});

test.describe("where a system's own few colours take the place of the page's", () => {
  test.use({ forcedColors: "active" });

  test("the chosen theme is underlined, which takes no colour", async ({
    chat,
    page,
  }) => {
    // The ground that sets the chosen one apart is gone there, with every colour of
    // the page's own.
    const lines = () =>
      chat.theme
        .locator("label")
        .evaluateAll((labels) =>
          labels.map(
            (label) =>
              `${label.textContent}: ${getComputedStyle(label).textDecorationLine}`,
          ),
        );
    await chat.open();

    await chat.chooseTheme("Light");
    expect(await lines()).toEqual([
      "System: none",
      "Light: underline",
      "Dark: none",
    ]);

    await chat.chooseTheme("Dark");
    expect(await lines()).toEqual([
      "System: none",
      "Light: none",
      "Dark: underline",
    ]);

    // And with the page's own colours, nothing is: the ground says it.
    await page.emulateMedia({ forcedColors: "none" });
    expect(await lines()).toEqual([
      "System: none",
      "Light: none",
      "Dark: none",
    ]);
  });
});

test.describe("under a dark system", () => {
  test.use({ colorScheme: "dark" });

  test("Light chosen is light, its code too, and System again is dark", async ({
    chat,
    page,
  }) => {
    test.slow();
    await anAnswerWithCodeAndADiagram(chat);
    const system = await asTheSystemIsSet(chat, "dark");

    await chat.chooseTheme("Light");

    await expect.poll(() => looksOf(chat), DRAWN).toEqual(system.light);
    // The keyword in the light theme's colour: Tailwind's own `dark` variant would
    // give it the dark theme's, as the system is set.
    const light = await looksOf(chat);
    expect(light.keyword.shown).toBe(light.keyword.light);
    await expect(page.locator("html")).toHaveAttribute("data-scheme", "light");

    await chat.chooseTheme("System");

    await expect.poll(() => looksOf(chat), DRAWN).toEqual(system.dark);
    await expect(page.locator("html")).not.toHaveAttribute("data-scheme");
  });
});

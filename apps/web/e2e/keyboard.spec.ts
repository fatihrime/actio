// Where the keyboard is, a rule of the page's styles says by the element it styles or
// by that element's neighbour, never by an element around it (ADR 0015, revision "the
// keyboard's place is never asked of an element around it"). As Tailwind writes a rule
// that asks an element around, Safari's engine goes through the whole page at every
// move of the keyboard, whichever two controls it is between. What that costs shows in
// that engine alone, which these tests don't run. They hold the two things the real
// page says in any engine: that a row of the list still shows its buttons to the
// keyboard, which only the built stylesheet decides, and that no rule of that
// stylesheet, the markdown library's classes included, asks so.
import { expect, test } from "./chat";

test("a row of the list shows its buttons, and makes room for them, while the keyboard is on it", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.converse("Bikes: how to fix a flat tyre?");
  await chat.converse("Bread without yeast?");

  // "Bread" is the conversation on the page, and listed first: its row shows its
  // buttons whatever the keyboard does. The row looked at is the other one.
  const link = chat.listed.filter({ hasText: "Bikes" });
  const rename = chat.list.getByRole("button", {
    name: "Rename “Bikes: how to fix a flat tyre?”",
  });
  const remove = chat.list.getByRole("button", {
    name: "Delete “Bikes: how to fix a flat tyre?”",
  });
  const buttons = rename.locator("xpath=..");
  const onThePage = chat.list.getByRole("button", {
    name: "Delete “Bread without yeast?”",
  });
  const shows = async (itsButtons: boolean) => {
    await expect(buttons).toHaveCSS("opacity", itsButtons ? "1" : "0");
    await expect(link).toHaveCSS("padding-right", itsButtons ? "64px" : "12px");
    await expect(onThePage.locator("xpath=..")).toHaveCSS("opacity", "1");
  };

  // The pointer and the keyboard on the box: neither is on the row.
  await chat.box.click();
  await shows(false);

  // The keyboard on the row's link, and on each of its buttons, by the Tab key.
  await link.focus();
  await shows(true);
  await page.keyboard.press("Tab");
  await expect(rename).toBeFocused();
  await shows(true);
  await page.keyboard.press("Tab");
  await expect(remove).toBeFocused();
  await shows(true);

  // And back, out of the row: on the row before it, this one's buttons are gone.
  await page.keyboard.press("Shift+Tab");
  await expect(rename).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(link).toBeFocused();
  await shows(true);
  await page.keyboard.press("Shift+Tab");
  await expect(onThePage).toBeFocused();
  await shows(false);
});

/** Whether a rule with this selector asks an element other than the one it styles
 * whether the keyboard is inside it: by `:focus-within`, or by a `:has()` with `:focus`
 * in it. What the styled element is asked itself is its own state, and so is the last
 * element that a `:has()` of its own names: `.x:has(~ .y:focus-within)` is let through.
 *
 * Stricter than what was measured. Three ways of asking cost nothing in Safari's engine
 * and are refused all the same: a neighbour's `:focus-within` (`peer-focus-within:`), a
 * parent's for its own children, and a group named by its class alone, outside the
 * `:where()` Tailwind puts it in, as a variant written by hand names it. Which spelling
 * an engine reads cheaply is nothing to rest the page on, and nothing of the page's is
 * written so. A fourth was not measured, and is refused too: a `:has()` that asks an
 * element inside the styled one, for one further in. To let one of the four through,
 * change this on purpose, with its figure in that engine under a long conversation. */
function asksAnElementAround(selector: string): boolean {
  // An escaped character is part of a name, also one written as its number with the
  // space that ends it, and what an attribute holds is no selector.
  const text = selector
    .replace(/\\(?:[0-9a-f]{1,6} ?|.)/gi, "_")
    .replace(/\[[^\]]*\]/g, "_");
  return outside(text, /,/).some((one) => {
    // A selector is one element after another, across combinators: the last is the
    // element the rule styles, and the others are around it.
    const elements = outside(one, /[\s>~+]/).filter((part) => part !== "");
    const styled = elements.pop() ?? "";
    return (
      elements.some(keyboardInside) ||
      // What the styled element is asked itself is its own state. What it holds in
      // brackets is selectors again, each read the same way: as Tailwind writes a
      // group, all of the selector stands inside one `:is()`.
      inBrackets(styled).some(({ holds }) => asksAnElementAround(holds))
    );
  });
}

/** Whether an element is asked whether the keyboard is inside it: by `:focus-within`,
 * however deep in brackets, or by a `:has()` with `:focus` in it. */
function keyboardInside(element: string): boolean {
  return (
    element.includes(":focus-within") ||
    inBrackets(element).some(({ name, holds }) =>
      name === "has" ? holds.includes(":focus") : keyboardInside(holds),
    )
  );
}

/** `text` cut at each character `cut` matches that stands outside every bracket. */
function outside(text: string, cut: RegExp): string[] {
  const parts: string[] = [];
  let part = "";
  let depth = 0;
  for (const character of text) {
    depth += character === "(" ? 1 : character === ")" ? -1 : 0;
    if (depth === 0 && cut.test(character)) {
      parts.push(part);
      part = "";
    } else {
      part += character;
    }
  }
  return [...parts, part];
}

/** What one element's part of a selector holds in brackets, the outermost of them: for
 * `.x:is(.a .b):has(.c)`, an `is` that holds `.a .b` and a `has` that holds `.c`. */
function inBrackets(element: string): { name: string; holds: string }[] {
  const found: { name: string; holds: string }[] = [];
  let depth = 0;
  let from = 0;
  for (let at = 0; at < element.length; at++) {
    if (element[at] === "(") {
      if (depth === 0) {
        from = at + 1;
      }
      depth++;
    } else if (element[at] === ")") {
      depth--;
      if (depth === 0) {
        const name = /:([a-z-]+)\($/.exec(element.slice(0, from))?.[1] ?? "";
        found.push({ name, holds: element.slice(from, at) });
      }
    }
  }
  return found;
}

/** What the check is held to before it reads a page: selectors it has to refuse, and
 * selectors it has to let through. Most are as Tailwind 4.3.5 writes them for the
 * variant named above each; the rest are written by hand, and say so. A figure is the
 * style pass of one move of the keyboard in Safari's engine under 600 messages, the
 * middle of sixteen moves, across four pairs of controls (ADR 0015).
 *
 * A variant is named here without what it would style, and a class is spelled whole
 * only as its selector: Tailwind writes a rule for every class it finds in a file of
 * the app's, this one included, and such a rule costs whether or not an element has
 * the class. Two classes named whole in these comments failed the test below. */
const REFUSED = [
  // `group-focus-within:`, 21 to 28 ms
  ":is(:where(.group):focus-within .group-focus-within\\:pr-16)",
  // `in-focus-within:`, 21 to 26 ms
  ":where(:focus-within) .in-focus-within\\:pr-16",
  // `group-has-focus-within:`, 44 to 49 ms
  ":is(:where(.group):has(:focus-within) .group-has-focus-within\\:pr-16)",
  // `group-has-focus:` and `group-has-[:focus]:`, 6 to 7 ms
  ":is(:where(.group):has(:focus) .group-has-focus\\:pr-16)",
  ":is(:where(.group):has(:is(:focus)) .group-has-\\[\\:focus\\]\\:pr-16)",
  // a variant written by hand, between square brackets, that asks the row: it costs
  // nothing spelled so, and is refused all the same
  ".group:focus-within .\\[\\.group\\:focus-within_\\&\\]\\:pr-16",
  // by hand: one of several, the second selector of a rule that has two, and an
  // element around named after the element styled
  ":is(.a:focus-within, .b) .c",
  ".a, .b:focus-within .c",
  ".c:where(:focus-within *)",
  // refused on purpose, though they cost nothing: a neighbour's, as `peer-focus-within:`
  // is written, and a parent's for its own children, without the spaces a stylesheet's
  // file leaves out
  ":is(:where(.peer):focus-within ~ .peer-focus-within\\:opacity-100)",
  ".a:focus-within>.c",
  // not measured, and refused: what an element inside the styled one holds
  ".c:has(.a:focus-within .b)",
];
const LET_THROUGH = [
  // `focus-within:`, the box's own line and the row's buttons
  ".focus-within\\:border-ring:focus-within",
  ".focus-within\\:opacity-100:focus-within",
  // `peer-focus:`, the row's buttons by the link before them
  ":is(:where(.peer):focus ~ .peer-focus\\:opacity-100)",
  // `focus:` and `has-[…]:`, the row's link
  ".focus\\:pr-16:focus",
  ".has-\\[\\~div\\:focus-within\\]\\:pr-16:has(~div:focus-within)",
  // `in-data-keys:has-focus:`, the choice of light or dark
  ":where([data-keys]) .in-data-keys\\:has-focus\\:outline-2:has(:focus)",
  // `group-hover:`: the pointer's place, which is another matter
  ":is(:where(.group):hover .group-hover\\:pr-16)",
  // an element's own state, in classes no component has: on a dark page, where the
  // brackets name what is around it and ask nothing of the keyboard there; of each
  // element inside another, whose class has the state in its name; and beside an
  // attribute with a space in it
  ".dark\\:focus-within\\:ring-2:where(:root[data-scheme=dark] *):focus-within",
  ":is(.\\*\\*\\:focus-within\\:underline *):focus-within",
  '.focus-within\\:aria-\\[label\\=a_b\\]\\:ring-2:focus-within[aria-label="a b"]',
  // by hand: an element's own state denied, and beside a class that begins with a
  // figure, which a stylesheet writes as its number and a space
  ".c:not(:focus-within)",
  ".c:focus-within.\\31 23",
  // rules that have nothing of the keyboard in them, the last two by hand
  ".dark\\:text-\\[var\\(--x\\)\\]:where(:root:not([data-scheme=light]) *)",
  ".space-y-3 > :not(:last-child)",
  ".c:nth-child(2n + 1)",
];

/** Rules the test puts on the page itself, to see that the reading finds one wherever a
 * stylesheet can hold it: plain, inside a media query and a layer, and nested in another
 * rule. The last asks the styled element alone. */
const PUT_ON_THE_PAGE = `
  .put-a:focus-within .put-b { opacity: 1 }
  @media (min-width: 1px) { @layer put { .put-c:focus-within .put-d { opacity: 1 } } }
  .put-e:focus-within { .put-f { opacity: 1 } }
  .put-g:focus-within { opacity: 1 }
`;

/** The selector of every style rule the page has, read in the page. A rule inside a
 * media query or a layer is read as any other, and a rule nested in another with its
 * parent's selector where its own says `&`: as it applies. */
function everySelector(): string[] {
  const found: string[] = [];
  const read = (rules: CSSRuleList, parent: string) => {
    for (const rule of Array.from(rules)) {
      if (rule instanceof CSSStyleRule) {
        const own = rule.selectorText.replace(/&/g, `:is(${parent})`);
        found.push(own);
        read(rule.cssRules, own);
      } else if (rule instanceof CSSGroupingRule) {
        read(rule.cssRules, parent);
      }
    }
  };
  for (const sheet of Array.from(document.styleSheets)) {
    read(sheet.cssRules, "");
  }
  return found;
}

test("no rule of the page's styles asks an element whether the keyboard is inside it, for the look of what it holds", async ({
  chat,
  page,
}) => {
  // The check itself first: what it has to refuse, and what it has to let through.
  expect(REFUSED.filter((selector) => !asksAnElementAround(selector))).toEqual(
    [],
  );
  expect(LET_THROUGH.filter(asksAnElementAround)).toEqual([]);

  // Every rule the page has once an answer is on it: the stylesheet is built from the
  // classes found in the app and in the markdown library, whichever of them a page
  // draws, and an answer brings whatever comes with the code that draws markdown.
  await chat.open();
  await chat.converse("Hello");
  const selectors = await page.evaluate(everySelector);
  expect(selectors.length).toBeGreaterThan(200);
  expect(selectors.filter(asksAnElementAround)).toEqual([]);

  // And the reading itself: of four rules put on this page, the three that ask an
  // element around are found, each as it applies.
  await page.addStyleTag({ content: PUT_ON_THE_PAGE });
  expect(
    (await page.evaluate(everySelector)).filter(asksAnElementAround),
  ).toEqual([
    ".put-a:focus-within .put-b",
    ".put-c:focus-within .put-d",
    ":is(.put-e:focus-within) .put-f",
  ]);
});

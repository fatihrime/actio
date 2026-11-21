import { renderToStaticMarkup } from "react-dom/server";
import { expect, test } from "vitest";

import Home from "./page";

// What the server sends before any script has run in the browser.
const html = renderToStaticMarkup(<Home />);

/** The page the server sends, as the browser reads it. */
function sent(): HTMLElement {
  const page = document.createElement("div");
  page.innerHTML = html;
  return page;
}

test("the page the server sends names the product", () => {
  expect(html).toMatch(/<h1[^>]*>Actio<\/h1>/);
});

test("the page the server sends holds nothing of anyone's", () => {
  // The server never asks the API for anything: it has no session to ask with. The
  // browser starts one, and only then is a conversation shown.
  expect(html).toContain("Starting…");
  expect(html).not.toContain("<textarea");
});

test("the page the server sends has the header's buttons in their places, out of reach", () => {
  // What arrives with the session fills places that were already there: nothing on
  // the page moves when it does (ADR 0015, revision of 2026-11-21).
  const page = sent();
  const [list, newChat, ...more] = page.querySelectorAll("header button");
  expect(more).toEqual([]);
  expect(list).toHaveAttribute("aria-label", "Conversations");
  expect(newChat).toHaveTextContent("New chat");
  for (const button of [list, newChat]) {
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-hidden", "true");
  }
  // Whether the list is shown is what only the browser knows: the server says
  // nothing of it, and the browser's first render says nothing either.
  expect(list).not.toHaveAttribute("aria-expanded");
  expect(list).not.toHaveAttribute("title");
});

test("the page the server sends has the list's place beside the chat, decided by CSS", () => {
  const page = sent();
  const place = page.querySelector("main")?.previousElementSibling;
  expect(place?.tagName).toBe("DIV");
  expect(place).toHaveAttribute("aria-hidden", "true");
  expect(place).toBeEmptyDOMElement();
  // Shown in a wide window only, unless the page's root says that the list was put
  // away (lib/frame.ts): the server knows neither the window nor what was put away.
  expect(place).toHaveClass("hidden", "md:block", "list-away:hidden", "w-72");
  expect(page.querySelector("aside, nav")).toBeNull();
});

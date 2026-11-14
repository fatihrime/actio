import { renderToStaticMarkup } from "react-dom/server";
import { expect, test } from "vitest";

import Home from "./page";

// What the server sends before any script has run in the browser.
const html = renderToStaticMarkup(<Home />);

test("the page the server sends names the product", () => {
  expect(html).toMatch(/<h1[^>]*>Actio<\/h1>/);
});

test("the page the server sends holds nothing of anyone's", () => {
  // The server never asks the API for anything: it has no session to ask with. The
  // browser starts one, and only then is a conversation shown.
  expect(html).toContain("Starting…");
  expect(html).not.toContain("<textarea");
});

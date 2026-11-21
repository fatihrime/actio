import { renderToStaticMarkup } from "react-dom/server";
import { expect, test } from "vitest";

import { LIST_AWAY_SCRIPT } from "@/lib/frame";

import RootLayout from "./layout";

test("the page's head holds the script that marks a list put away, and no other", () => {
  // It has to run before the first paint, while the page is read: in the head, as
  // the browser gets it from the server (lib/frame.ts).
  const page = new DOMParser().parseFromString(
    renderToStaticMarkup(
      <RootLayout>
        <p>The page</p>
      </RootLayout>,
    ),
    "text/html",
  );

  const scripts = [...page.querySelectorAll("script")];
  expect(scripts.map((script) => script.parentElement?.tagName)).toEqual([
    "HEAD",
  ]);
  expect(scripts[0]?.textContent).toBe(LIST_AWAY_SCRIPT);
  expect(page.body.textContent).toBe("The page");
});

test("the page's root lets the script's mark be", () => {
  // The script adds an attribute that React didn't render. Without this, React says at
  // every load in development that the page it took over doesn't match, for whoever
  // put the list away: seen under `next dev` in Chromium, Firefox and WebKit. The
  // attribute is React's own and never reaches the HTML, so it is read off the
  // element the layout makes.
  const root = RootLayout({ children: null });

  expect(root.type).toBe("html");
  expect(root.props).toMatchObject({ suppressHydrationWarning: true });
});

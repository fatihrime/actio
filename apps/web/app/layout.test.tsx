import { renderToStaticMarkup } from "react-dom/server";
import { expect, test } from "vitest";

import { FRAME_SCRIPT } from "@/lib/frame";

import RootLayout from "./layout";

test("the page's body begins with the script that marks the frame, and holds no other", () => {
  // It has to run before the first paint, while the page is read, as the browser gets
  // it from the server (lib/frame.ts): before anything of the page's content. Not in
  // the head, where it held back the page's own scripts in Chromium until the
  // stylesheet was in (ADR 0015, revision of 2026-11-22).
  const page = new DOMParser().parseFromString(
    renderToStaticMarkup(
      <RootLayout>
        <p>The page</p>
      </RootLayout>,
    ),
    "text/html",
  );

  const scripts = [...page.querySelectorAll("script")];
  expect(scripts).toHaveLength(1);
  expect(scripts[0]?.textContent).toBe(FRAME_SCRIPT);
  expect(page.head.querySelector("script")).toBeNull();
  expect([...page.body.children].map((child) => child.tagName)).toEqual([
    "SCRIPT",
    "P",
  ]);
  expect(page.body.lastElementChild?.textContent).toBe("The page");
});

test("the page's root lets the script's marks be", () => {
  // The script adds attributes that React didn't render. Without this, React says at
  // every load in development that the page it took over doesn't match, for whoever
  // put the list away: seen under `next dev` in Chromium, Firefox and WebKit. The
  // attribute is React's own and never reaches the HTML, so it is read off the
  // element the layout makes.
  const root = RootLayout({ children: null });

  expect(root.type).toBe("html");
  expect(root.props).toMatchObject({ suppressHydrationWarning: true });
});

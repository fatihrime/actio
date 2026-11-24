// Runs before every test file.
import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { LIST_AWAY_MARK, SCHEME_MARK } from "@/lib/frame";

// How long a test waits for something to appear, at the most. A stored conversation,
// and an answer's words, are shown once the code that reads markdown is here (ADR 0016,
// revisions of 2026-11-21 and "an answer that arrives waits for the renderer too"), and
// the first test of a file to show one waits for that code to be loaded: about half a
// second on a developer's machine, 0.8 to 1.0 s with every core busy, which is all of
// the library's own one second. A test that passes takes no longer for this.
configure({ asyncUtilTimeout: 3000 });

// A test file that runs in Node instead of the simulated page (`@vitest-environment
// node`, as lib/chat/colouring.test.ts does) has no page to set up.
const onPage = typeof window !== "undefined";

// The simulated page lays nothing out, so nothing on it ever changes size, and it has
// nothing to tell so. The chat asks to be told (components/chat/follow.ts).
if (onPage) {
  window.ResizeObserver = class {
    observe() {
      // Nothing will be resized.
    }
    unobserve() {
      // Nothing was observed.
    }
    disconnect() {
      // Nothing was observed.
    }
  };
}

// Take each test's components off the page, so the next test starts with an empty one.
afterEach(() => {
  if (onPage) {
    cleanup();
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/");
    // The marks of a list put away and of the scheme chosen are on the page's root
    // element (lib/frame.ts), which outlives a test's components.
    document.documentElement.removeAttribute(LIST_AWAY_MARK);
    document.documentElement.removeAttribute(SCHEME_MARK);
  }
  vi.useRealTimers(); // a test may have set the clock (./clock.ts)
});

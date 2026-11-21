// Runs before every test file.
import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// How long a test waits for something to appear, at the most. A stored conversation
// is shown once the code that reads markdown is here (ADR 0016, revision of
// 2026-11-21), and the first test of a file to open one waits for that code to be
// loaded: about half a second on a developer's machine, 0.8 to 1.0 s with every core
// busy, which is all of the library's own one second. A test that passes takes no
// longer for this.
configure({ asyncUtilTimeout: 3000 });

// The simulated page lays nothing out, so nothing on it ever changes size, and it has
// nothing to tell so. The chat asks to be told (components/chat/follow.ts).
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

// Take each test's components off the page, so the next test starts with an empty one.
afterEach(() => {
  cleanup();
  window.localStorage.clear();
  window.history.replaceState(null, "", "/");
  vi.useRealTimers(); // a test may have set the clock (./clock.ts)
});

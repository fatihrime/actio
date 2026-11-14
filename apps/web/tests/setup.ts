// Runs before every test file.
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

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
});

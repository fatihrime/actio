// The script at the start of the page's body, run as the browser runs it: an element of
// the page, which the simulated browser runs as soon as it is in the page.
import { describe, expect, it, vi } from "vitest";

import {
  FRAME_SCRIPT,
  LIST_AWAY_MARK,
  LIST_HIDDEN_KEY,
  markListAway,
  markScheme,
  SCHEME_KEY,
  SCHEME_MARK,
} from "./frame";

/** Run the text the layout sends, as a script at the start of the page's body, and
 * hand back what it threw. */
function runTheScript(): unknown[] {
  const thrown: unknown[] = [];
  const caught = (event: ErrorEvent) => {
    thrown.push(event.error);
  };
  window.addEventListener("error", caught);
  const script = document.createElement("script");
  script.textContent = FRAME_SCRIPT;
  document.body.prepend(script);
  script.remove();
  window.removeEventListener("error", caught);
  return thrown;
}

const root = () => document.documentElement;

describe("the script at the start of the page's body", () => {
  it("names what people's browsers hold", () => {
    // Browsers remember the switch and the scheme under these names, and the styles
    // look for these attributes: renamed, either would be lost.
    expect(LIST_HIDDEN_KEY).toBe("actio.conversations.hidden");
    expect(LIST_AWAY_MARK).toBe("data-list-away");
    expect(SCHEME_KEY).toBe("actio.scheme");
    expect(SCHEME_MARK).toBe("data-scheme");
  });

  it("marks the page's root when the list was put away", () => {
    window.localStorage.setItem(LIST_HIDDEN_KEY, "on");

    expect(runTheScript()).toEqual([]);

    expect(root()).toHaveAttribute(LIST_AWAY_MARK, "");
    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });

  it.each([
    ["shown again", "off"],
    ["never put away", null],
  ])("leaves the page's root unmarked when the list was %s", (_, stored) => {
    if (stored !== null) {
      window.localStorage.setItem(LIST_HIDDEN_KEY, stored);
    }

    expect(runTheScript()).toEqual([]);

    expect(root()).not.toHaveAttribute(LIST_AWAY_MARK);
  });

  it.each(["light", "dark"])(
    "marks the page's root with the scheme chosen: %s",
    (scheme) => {
      window.localStorage.setItem(SCHEME_KEY, scheme);

      expect(runTheScript()).toEqual([]);

      expect(root()).toHaveAttribute(SCHEME_MARK, scheme);
      expect(root()).not.toHaveAttribute(LIST_AWAY_MARK);
    },
  );

  it.each([
    ["nothing chosen", null],
    ["the system's", "system"],
    ["another colour", "blue"],
    ["nothing written", ""],
    ["a scheme written otherwise", "DARK"],
  ])("leaves the page's root without a scheme for %s", (_, stored) => {
    if (stored !== null) {
      window.localStorage.setItem(SCHEME_KEY, stored);
    }

    expect(runTheScript()).toEqual([]);

    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });

  it("sets both marks, for a list put away and a dark page", () => {
    window.localStorage.setItem(LIST_HIDDEN_KEY, "on");
    window.localStorage.setItem(SCHEME_KEY, "dark");

    expect(runTheScript()).toEqual([]);

    expect(root()).toHaveAttribute(LIST_AWAY_MARK, "");
    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");
  });

  it("marks a list put away beside a scheme that is none", () => {
    window.localStorage.setItem(LIST_HIDDEN_KEY, "on");
    window.localStorage.setItem(SCHEME_KEY, "blue");

    expect(runTheScript()).toEqual([]);

    expect(root()).toHaveAttribute(LIST_AWAY_MARK, "");
    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });

  it("throws nothing, and marks nothing, when the browser's storage is off limits", () => {
    window.localStorage.setItem(LIST_HIDDEN_KEY, "on");
    window.localStorage.setItem(SCHEME_KEY, "dark");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("The operation is insecure.", "SecurityError");
    });

    expect(runTheScript()).toEqual([]);

    expect(root()).not.toHaveAttribute(LIST_AWAY_MARK);
    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });
});

describe("the marks", () => {
  it("of the list are set and taken away by the page", () => {
    markListAway(true);
    expect(root()).toHaveAttribute(LIST_AWAY_MARK);

    markListAway(false);
    expect(root()).not.toHaveAttribute(LIST_AWAY_MARK);
  });

  it("of the scheme are set by the page, and taken away for the system's", () => {
    markScheme("dark");
    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");

    markScheme("light");
    expect(root()).toHaveAttribute(SCHEME_MARK, "light");

    markScheme("system");
    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });
});

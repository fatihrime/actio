// The script in the page's head, run as the browser runs it: an element of the page,
// which the simulated browser runs as soon as it is in the page.
import { describe, expect, it, vi } from "vitest";

import {
  LIST_AWAY_MARK,
  LIST_AWAY_SCRIPT,
  LIST_HIDDEN_KEY,
  markListAway,
} from "./frame";

/** Run the text the layout sends, as a script in the page's head, and hand back what
 * it threw. */
function runTheScript(): unknown[] {
  const thrown: unknown[] = [];
  const caught = (event: ErrorEvent) => {
    thrown.push(event.error);
  };
  window.addEventListener("error", caught);
  const script = document.createElement("script");
  script.textContent = LIST_AWAY_SCRIPT;
  document.head.append(script);
  script.remove();
  window.removeEventListener("error", caught);
  return thrown;
}

const root = () => document.documentElement;

describe("the script in the page's head", () => {
  it("names what people's browsers hold", () => {
    // Browsers remember the switch under this name, and the styles look for this
    // attribute: renamed, either would be lost.
    expect(LIST_HIDDEN_KEY).toBe("actio.conversations.hidden");
    expect(LIST_AWAY_MARK).toBe("data-list-away");
  });

  it("marks the page's root when the list was put away", () => {
    window.localStorage.setItem(LIST_HIDDEN_KEY, "on");

    expect(runTheScript()).toEqual([]);

    expect(root()).toHaveAttribute(LIST_AWAY_MARK, "");
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

  it("throws nothing, and marks nothing, when the browser's storage is off limits", () => {
    window.localStorage.setItem(LIST_HIDDEN_KEY, "on");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("The operation is insecure.", "SecurityError");
    });

    expect(runTheScript()).toEqual([]);

    expect(root()).not.toHaveAttribute(LIST_AWAY_MARK);
  });
});

describe("the mark", () => {
  it("is set and taken away by the page", () => {
    markListAway(true);
    expect(root()).toHaveAttribute(LIST_AWAY_MARK);

    markListAway(false);
    expect(root()).not.toHaveAttribute(LIST_AWAY_MARK);
  });
});

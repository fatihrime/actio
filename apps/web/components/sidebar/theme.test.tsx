// The choice of a light or a dark page, under the list of conversations, driven as a
// person would: through the whole page, against the fake API (tests/fake-api.ts). What
// CSS makes of it only a real browser shows (e2e/scheme.spec.ts).
import { act, cleanup, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SCHEME_KEY, SCHEME_MARK } from "@/lib/frame";
import { fakeApi } from "@/tests/fake-api";
import { narrowWindow, open, runFrameScript } from "@/tests/open";

const root = () => document.documentElement;
const remembered = () => window.localStorage.getItem(SCHEME_KEY);

async function theChoice(): Promise<HTMLElement> {
  return screen.findByRole("group", { name: "Theme" });
}

function radio(group: HTMLElement, name: string): HTMLElement {
  return within(group).getByRole("radio", { name });
}

/** A choice's words, which is where a person clicks: the radio button itself is out
 * of sight, under them. */
function words(group: HTMLElement, name: string): HTMLElement {
  return within(group).getByText(name);
}

/** Another window of this browser chooses `stored`, or clears the storage with
 * `null`, and this window is told so, as the browser tells it. */
function inAnotherWindow(stored: string | null): void {
  if (stored === null) {
    window.localStorage.clear();
  } else {
    window.localStorage.setItem(SCHEME_KEY, stored);
  }
  act(() => {
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: stored === null ? null : SCHEME_KEY,
        newValue: stored,
        storageArea: window.localStorage,
      }),
    );
  });
}

describe("the choice of a light or dark page", () => {
  it("stands under the list, as the system is set until the reader chooses", async () => {
    fakeApi();
    const user = open();

    const group = await theChoice();
    const list = screen.getByRole("navigation", { name: "Conversations" });
    // Under the list, and the last thing in its column, after the usage meter.
    expect(
      list.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      screen
        .getByRole("region", { name: "Usage" })
        .compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByRole("complementary").lastElementChild).toContainElement(
      group,
    );
    expect(within(group).getAllByRole("radio")).toEqual(
      ["System", "Light", "Dark"].map((name) => radio(group, name)),
    );
    expect(radio(group, "System")).toBeChecked();
    expect(root()).not.toHaveAttribute(SCHEME_MARK);

    await user.click(words(group, "Dark"));

    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");
    expect(remembered()).toBe("dark");
    expect(radio(group, "Dark")).toBeChecked();
    expect(radio(group, "System")).not.toBeChecked();
    // The keyboard stays on what was chosen, where the browser puts it there: the
    // simulated one does, as Chromium and Firefox do, and WebKit doesn't.
    expect(radio(group, "Dark")).toHaveFocus();

    await user.click(words(group, "System"));

    expect(root()).not.toHaveAttribute(SCHEME_MARK);
    expect(remembered()).toBeNull();
    expect(radio(group, "System")).toBeChecked();
    expect(radio(group, "System")).toHaveFocus();
  });

  it("is one stop for the keyboard, where an arrow key chooses the next", async () => {
    fakeApi();
    const user = open();
    const group = await theChoice();
    // What comes before it: the guest's link to sign up, under the usage meter.
    within(screen.getByRole("complementary"))
      .getByRole("link", { name: "Sign up" })
      .focus();

    await user.tab();
    expect(radio(group, "System")).toHaveFocus();
    await user.tab();
    expect(group).not.toContainElement(document.activeElement as HTMLElement);
    await user.tab({ shift: true });
    expect(radio(group, "System")).toHaveFocus();

    await user.keyboard("{ArrowRight}");

    expect(radio(group, "Light")).toBeChecked();
    expect(radio(group, "Light")).toHaveFocus();
    expect(root()).toHaveAttribute(SCHEME_MARK, "light");

    await user.keyboard("{ArrowDown}");

    expect(radio(group, "Dark")).toBeChecked();
    expect(radio(group, "Dark")).toHaveFocus();
    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");
    expect(remembered()).toBe("dark");
  });

  it("notes that an arrow key was used in it, for the outline, until the mouse is", async () => {
    // WebKit doesn't say that the keyboard is in use of a radio button an arrow key
    // moved to, so the styles go by the group's own note as well (theme.tsx). What
    // they draw only a real browser shows (e2e/scheme.spec.ts).
    fakeApi();
    const user = open();
    const group = await theChoice();
    expect(group).not.toHaveAttribute("data-keys");

    // A key held for something else, as for a shortcut, is no arrow.
    radio(group, "System").focus();
    await user.keyboard("{Control>}c{/Control}{Shift}");
    expect(group).not.toHaveAttribute("data-keys");

    await user.keyboard("{ArrowRight}");

    expect(radio(group, "Light")).toHaveFocus();
    expect(group).toHaveAttribute("data-keys");

    await user.click(words(group, "Dark"));

    expect(radio(group, "Dark")).toBeChecked();
    expect(group).not.toHaveAttribute("data-keys");
  });

  it("shows what this browser remembers when the page has lost its mark", async () => {
    // As when React couldn't take the page over as the server sent it, drew it anew,
    // and took every attribute off the root, the script's mark with them: the page
    // reads the choice once more when it starts to follow those made elsewhere.
    window.localStorage.setItem(SCHEME_KEY, "dark");
    fakeApi();

    open();

    const group = await theChoice();
    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");
    expect(radio(group, "Dark")).toBeChecked();
  });

  it("follows nothing once the app has left the page", async () => {
    fakeApi();
    open();
    await theChoice();

    cleanup();
    inAnotherWindow("dark");

    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });

  it("says what was chosen on an earlier visit", async () => {
    window.localStorage.setItem(SCHEME_KEY, "dark");
    runFrameScript(); // as the browser starts the page
    fakeApi();

    open();

    const group = await theChoice();
    expect(radio(group, "Dark")).toBeChecked();
    expect(radio(group, "System")).not.toBeChecked();
    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");
  });

  it.each([
    ["", false],
    [", in StrictMode as in development", true],
  ])(
    "follows a choice made in another window, with the list put away and nothing on the page that asks%s",
    async (_, strict) => {
      fakeApi();
      const user = open("/", { strict });
      await theChoice();
      await user.click(screen.getByRole("button", { name: "Conversations" }));
      expect(
        screen.queryByRole("group", { name: "Theme" }),
      ).not.toBeInTheDocument();

      inAnotherWindow("dark");
      expect(root()).toHaveAttribute(SCHEME_MARK, "dark");

      inAnotherWindow("light");
      expect(root()).toHaveAttribute(SCHEME_MARK, "light");

      inAnotherWindow(null);
      expect(root()).not.toHaveAttribute(SCHEME_MARK);

      // Shown again, the choice says what was chosen meanwhile.
      inAnotherWindow("dark");
      await user.click(screen.getByRole("button", { name: "Conversations" }));
      expect(radio(await theChoice(), "Dark")).toBeChecked();
    },
  );

  it("is in the list that lies over the chat in a narrow window", async () => {
    narrowWindow();
    fakeApi();
    const user = open();

    await user.click(
      await screen.findByRole("button", { name: "Conversations" }),
    );

    const over = screen.getByRole("complementary");
    expect(
      within(over).getByRole("group", { name: "Theme" }),
    ).toBeInTheDocument();
    await user.click(words(await theChoice(), "Light"));
    expect(root()).toHaveAttribute(SCHEME_MARK, "light");
  });
});

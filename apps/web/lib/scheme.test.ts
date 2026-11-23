// Light or dark, as the reader chose or as their system is set (lib/scheme.ts): what the
// page's components are told, and what the page's root says, which CSS shows.
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, onTestFinished, vi } from "vitest";

import { runFrameScript } from "@/tests/open";

import { LIST_HIDDEN_KEY, SCHEME_KEY, SCHEME_MARK } from "./frame";
import { followChoicesMadeElsewhere, useDarkScheme, useScheme } from "./scheme";
import type { Scheme } from "./scheme";

const root = () => document.documentElement;
const remembered = () => window.localStorage.getItem(SCHEME_KEY);

/** A system set light or dark, which the test can turn: the page asks it through
 * `matchMedia`, which answers by the query, and is told when the setting changes. */
function aSystem(setting: "light" | "dark"): {
  turn: (next: "light" | "dark") => void;
} {
  let now = setting;
  const told = new Set<() => void>();
  vi.stubGlobal("matchMedia", (query: string) => {
    const asksDark = query === "(prefers-color-scheme: dark)";
    return {
      media: query,
      matches: asksDark && now === "dark",
      addEventListener: (_: string, changed: () => void) => {
        if (asksDark) {
          told.add(changed);
        }
      },
      removeEventListener: (_: string, changed: () => void) => {
        told.delete(changed);
      },
    };
  });
  return {
    turn: (next) => {
      now = next;
      act(() => {
        for (const changed of told) {
          changed();
        }
      });
    },
  };
}

/** Choose `scheme`, as the page's control does. */
function choose(scheme: Scheme): void {
  const { result, unmount } = renderHook(() => useScheme());
  act(() => {
    result.current[1](scheme);
  });
  unmount();
}

/** Follow the choices made elsewhere for the rest of the test, as the app does while
 * it is on the page. */
function followThem(): void {
  onTestFinished(followChoicesMadeElsewhere());
}

/** Another window of this browser remembers `stored` for the scheme, or clears the
 * storage with `null`, and this window is told so. */
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

describe("the scheme chosen", () => {
  it.each([
    ["nothing remembered", null, "system"],
    ["light remembered", "light", "light"],
    ["dark remembered", "dark", "dark"],
    ["something else remembered", "blue", "system"],
  ])(
    "is what the page started with: %s",
    (_, stored: string | null, expected) => {
      if (stored !== null) {
        window.localStorage.setItem(SCHEME_KEY, stored);
      }
      runFrameScript();

      const { result } = renderHook(() => useScheme());

      expect(result.current[0]).toBe(expected);
    },
  );

  it("is what the page shows, though the storage says otherwise since", () => {
    // Another window chose, and this one wasn't told: what the components say and
    // what CSS shows stay one.
    window.localStorage.setItem(SCHEME_KEY, "dark");
    runFrameScript();
    window.localStorage.setItem(SCHEME_KEY, "light");

    const { result } = renderHook(() => useScheme());

    expect(result.current[0]).toBe("dark");
    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");
  });

  it("is remembered and shown once chosen, and every reader is told", () => {
    const one = renderHook(() => useScheme());
    const another = renderHook(() => useScheme());

    act(() => {
      one.result.current[1]("dark");
    });

    expect(one.result.current[0]).toBe("dark");
    expect(another.result.current[0]).toBe("dark");
    expect(remembered()).toBe("dark");
    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");

    act(() => {
      another.result.current[1]("light");
    });

    expect(one.result.current[0]).toBe("light");
    expect(remembered()).toBe("light");
    expect(root()).toHaveAttribute(SCHEME_MARK, "light");

    act(() => {
      one.result.current[1]("system");
    });

    expect(one.result.current[0]).toBe("system");
    expect(another.result.current[0]).toBe("system");
    expect(remembered()).toBeNull();
    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });

  it("is shown, and not remembered, when the browser's storage is off limits", () => {
    followThem();
    for (const method of ["getItem", "setItem", "removeItem"] as const) {
      vi.spyOn(Storage.prototype, method).mockImplementation(() => {
        throw new DOMException("The operation is insecure.", "SecurityError");
      });
    }
    const { result } = renderHook(() => useScheme());
    const dark = renderHook(() => useDarkScheme());

    act(() => {
      result.current[1]("dark");
    });

    expect(result.current[0]).toBe("dark");
    expect(dark.result.current).toBe(true);
    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");

    // Shown again from the back/forward cache: what can't be read changes nothing.
    act(() => {
      window.dispatchEvent(
        new PageTransitionEvent("pageshow", { persisted: true }),
      );
    });

    expect(result.current[0]).toBe("dark");

    act(() => {
      result.current[1]("system");
    });

    expect(result.current[0]).toBe("system");
    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });
});

describe("whether the page is shown dark", () => {
  it.each([
    ["system", "light", false],
    ["system", "dark", true],
    ["light", "light", false],
    ["light", "dark", false],
    ["dark", "light", true],
    ["dark", "dark", true],
  ] as const)(
    "with %s chosen and the system set %s: %s",
    (chosen, setting, expected) => {
      aSystem(setting);
      choose(chosen);

      const { result } = renderHook(() => useDarkScheme());

      expect(result.current).toBe(expected);
    },
  );

  it("follows the system while the choice is the system's", () => {
    const system = aSystem("light");
    const { result } = renderHook(() => useDarkScheme());
    expect(result.current).toBe(false);

    system.turn("dark");
    expect(result.current).toBe(true);

    system.turn("light");
    expect(result.current).toBe(false);
  });

  it.each([
    ["light", false],
    ["dark", true],
  ] as const)(
    "doesn't follow the system while %s is chosen",
    (chosen, expected) => {
      const system = aSystem(chosen === "dark" ? "light" : "dark");
      choose(chosen);
      const { result } = renderHook(() => useDarkScheme());
      expect(result.current).toBe(expected);

      system.turn(chosen === "dark" ? "dark" : "light");
      expect(result.current).toBe(expected);

      system.turn(chosen === "dark" ? "light" : "dark");
      expect(result.current).toBe(expected);
    },
  );

  it("follows a choice as soon as it is made", () => {
    aSystem("light");
    const { result } = renderHook(() => useDarkScheme());
    expect(result.current).toBe(false);

    choose("dark");
    expect(result.current).toBe(true);

    choose("system");
    expect(result.current).toBe(false);
  });
});

describe("a choice made elsewhere", () => {
  it("in another window is shown, and every reader is told", () => {
    followThem();
    const { result } = renderHook(() => useScheme());
    const dark = renderHook(() => useDarkScheme());

    inAnotherWindow("dark");

    expect(result.current[0]).toBe("dark");
    expect(dark.result.current).toBe(true);
    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");

    inAnotherWindow("light");

    expect(result.current[0]).toBe("light");
    expect(dark.result.current).toBe(false);
    expect(root()).toHaveAttribute(SCHEME_MARK, "light");
  });

  it("is the system's again when another window cleared the storage", () => {
    followThem();
    choose("dark");
    const { result } = renderHook(() => useScheme());

    inAnotherWindow(null);

    expect(result.current[0]).toBe("system");
    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });

  it("is the system's when another window remembers what is no scheme", () => {
    followThem();
    choose("dark");
    const { result } = renderHook(() => useScheme());

    inAnotherWindow("blue");

    expect(result.current[0]).toBe("system");
    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });

  it.each([
    {
      apart:
        "another window chose after the page's script had read the storage",
      marked: "dark",
      remembered: "light",
      shown: "light",
    },
    {
      apart: "the mark the page's script set was taken off the root",
      marked: null,
      remembered: "dark",
      shown: "dark",
    },
    {
      apart:
        "another window chose the system's after the page's script had run",
      marked: "dark",
      remembered: null,
      shown: "system",
    },
  ] as const)(
    "is read once when the following starts: $apart",
    ({ marked, remembered: stored, shown }) => {
      // Where the mark and the storage have come apart by then, of which no event
      // tells the page.
      if (marked !== null) {
        root().setAttribute(SCHEME_MARK, marked);
      }
      if (stored !== null) {
        window.localStorage.setItem(SCHEME_KEY, stored);
      }
      const { result } = renderHook(() => useScheme());
      expect(result.current[0]).toBe(marked ?? "system");

      act(followThem);

      expect(result.current[0]).toBe(shown);
      if (shown === "system") {
        expect(root()).not.toHaveAttribute(SCHEME_MARK);
      } else {
        expect(root()).toHaveAttribute(SCHEME_MARK, shown);
      }
    },
  );

  it("writes nothing on the page's root where the mark says what is remembered", () => {
    // The mark changes only where the choice does: a mark written again, with what it
    // said already, would be one more change of the root while React takes the page
    // over (components/chat/frame.test.tsx).
    window.localStorage.setItem(SCHEME_KEY, "dark");
    runFrameScript();
    const written = new MutationObserver(() => undefined);
    written.observe(root(), { attributes: true });
    onTestFinished(() => {
      written.disconnect();
    });

    followThem();
    window.dispatchEvent(
      new PageTransitionEvent("pageshow", { persisted: true }),
    );

    expect(written.takeRecords()).toEqual([]);
    expect(root()).toHaveAttribute(SCHEME_MARK, "dark");
  });

  it("is read again when the browser shows the page again from its back/forward cache", () => {
    followThem();
    const { result } = renderHook(() => useScheme());
    // Chosen elsewhere while the page was away, which it wasn't told of.
    window.localStorage.setItem(SCHEME_KEY, "light");

    // A page shown for the first time says so too: its script has read the storage.
    act(() => {
      window.dispatchEvent(
        new PageTransitionEvent("pageshow", { persisted: false }),
      );
    });
    expect(result.current[0]).toBe("system");

    act(() => {
      window.dispatchEvent(
        new PageTransitionEvent("pageshow", { persisted: true }),
      );
    });

    expect(result.current[0]).toBe("light");
    expect(root()).toHaveAttribute(SCHEME_MARK, "light");
  });

  it("changes nothing when another window changed something else", () => {
    followThem();
    const { result } = renderHook(() => useScheme());
    window.localStorage.setItem(SCHEME_KEY, "dark");

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: LIST_HIDDEN_KEY,
          newValue: "on",
          storageArea: window.localStorage,
        }),
      );
    });

    expect(result.current[0]).toBe("system");
    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });

  it("is no longer followed once the following has ended", () => {
    const stop = followChoicesMadeElsewhere();
    const { result } = renderHook(() => useScheme());

    stop();
    inAnotherWindow("dark");

    expect(result.current[0]).toBe("system");
    expect(root()).not.toHaveAttribute(SCHEME_MARK);

    // Nor when the browser shows the page again.
    act(() => {
      window.dispatchEvent(
        new PageTransitionEvent("pageshow", { persisted: true }),
      );
    });

    expect(result.current[0]).toBe("system");
    expect(root()).not.toHaveAttribute(SCHEME_MARK);
  });
});

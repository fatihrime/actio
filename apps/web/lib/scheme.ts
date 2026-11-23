// Light or dark: as the reader chose, or as their system is set, the default. The
// choice is this browser's, remembered in its storage, like the switch that puts the
// list away, and not the account's: the page has to know it before its first paint,
// before it knows whose session it has (ADR 0015, revision "the reader chooses light or
// dark").
//
// What the page shows is what its root element's mark says (lib/frame.ts). The script
// at the start of the body sets it from the browser's storage, before the first paint,
// and CSS does the rest (app/globals.css). So the mark is the page's choice, and the
// storage what the browser remembers of it: the hooks here read the mark, and the mark
// changes only where the choice changes. When the reader chooses, here; when a choice
// made elsewhere arrives, and once when the page starts to follow those, in case the
// mark and the storage have come apart by then (`followChoicesMadeElsewhere`). Never
// from an effect that follows a hook: while React takes the page over, a hook answers
// with the server's "system", and the mark the script set would be taken away for a
// moment.
//
// This module is the one place that knows the choice. What a script draws, such as a
// diagram, asks `useDarkScheme`; nothing else asks the system's setting.
import { useSyncExternalStore } from "react";

import { markScheme, SCHEME_KEY, SCHEME_MARK } from "./frame";
import { useMedia } from "./media";

/** As the system is set, light, or dark. */
export type Scheme = "system" | "light" | "dark";

/** The query for a system that asks for a dark page. */
const SYSTEM_DARK = "(prefers-color-scheme: dark)";

/** Whatever on the page reads the choice, to be told when it changes. */
const readers = new Set<() => void>();

function subscribe(changed: () => void): () => void {
  readers.add(changed);
  return () => {
    readers.delete(changed);
  };
}

/** A scheme as text says it, in the browser's storage or in the mark: anything but
 * the two is the system's. */
function schemeIn(text: string | null): Scheme {
  return text === "light" || text === "dark" ? text : "system";
}

/** The scheme the page shows now. */
function shown(): Scheme {
  return schemeIn(document.documentElement.getAttribute(SCHEME_MARK));
}

/** The scheme while the server prepares the page, and while React takes over what it
 * sent: nobody can say there. */
function onTheServer(): Scheme {
  return "system";
}

/** Show `scheme`, and tell whatever reads the choice. */
function show(scheme: Scheme): void {
  markScheme(scheme);
  for (const changed of readers) {
    changed();
  }
}

/** Choose `scheme`: shown at once, and remembered in this browser where its storage
 * can be written. */
function choose(scheme: Scheme): void {
  try {
    if (scheme === "system") {
      window.localStorage.removeItem(SCHEME_KEY);
    } else {
      window.localStorage.setItem(SCHEME_KEY, scheme);
    }
  } catch {
    // Storage is off limits, as in some private windows: this page shows the choice,
    // and the next one won't know it.
  }
  show(scheme);
}

/** The scheme the reader chose, and a way to choose another: the same answer in every
 * component that asks, kept current. "system" where nobody can say, as while the
 * server prepares the page. */
export function useScheme(): [Scheme, (scheme: Scheme) => void] {
  const scheme = useSyncExternalStore(subscribe, shown, onTheServer);
  return [scheme, choose];
}

/** Whether the page is shown dark now: as the reader chose, or while the choice is the
 * system's, as the system is set, which it follows when the system changes. For what
 * CSS can't reach, such as a diagram drawn by a script. */
export function useDarkScheme(): boolean {
  const [scheme] = useScheme();
  const systemDark = useMedia(SYSTEM_DARK, false);
  return scheme === "system" ? systemDark : scheme === "dark";
}

/** Follow the choices made where this page can't see them: in another window of this
 * browser, and while the browser kept this page away, in its back/forward cache, where
 * it was told of nothing. The choice is then read again from the browser's storage,
 * shown, and told. Hands back what ends the following.
 *
 * It is read once at the start too. By then the mark and the storage may have come
 * apart: another window chose after the script at the page's start had read the
 * storage, which no event tells this page of; or React could not take the page over
 * as the server sent it, as when a browser's extension changed it first, drew it anew,
 * and took every attribute off the root, the script's mark with them. */
export function followChoicesMadeElsewhere(): () => void {
  function stored(event: StorageEvent) {
    // A storage that was cleared says so with no key.
    if (event.key === SCHEME_KEY || event.key === null) {
      readAgain();
    }
  }
  function shownAgain(event: PageTransitionEvent) {
    if (event.persisted) {
      readAgain();
    }
  }
  window.addEventListener("storage", stored);
  window.addEventListener("pageshow", shownAgain);
  readAgain();
  return () => {
    window.removeEventListener("storage", stored);
    window.removeEventListener("pageshow", shownAgain);
  };
}

/** Show what this browser remembers, unless its storage can't be read: then the page
 * keeps what it shows. Where the page shows that already, nothing is written and
 * nobody is told: the mark changes only where the choice does. */
function readAgain(): void {
  let remembered: string | null;
  try {
    remembered = window.localStorage.getItem(SCHEME_KEY);
  } catch {
    return;
  }
  const scheme = schemeIn(remembered);
  if (scheme !== shown()) {
    show(scheme);
  }
}

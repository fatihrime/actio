"use client";

import { useState } from "react";

import { useScheme } from "@/lib/scheme";
import type { Scheme } from "@/lib/scheme";

const CHOICES: { scheme: Scheme; label: string }[] = [
  { scheme: "system", label: "System" },
  { scheme: "light", label: "Light" },
  { scheme: "dark", label: "Dark" },
];

/** Whether the page is light or dark: as the system is set, or as the reader chooses
 * (lib/scheme.ts). It stands under the list of conversations, which comes with the
 * session, so that nothing the server sends depends on what only this browser
 * remembers (ADR 0015, revision "the reader chooses light or dark").
 *
 * Three of the browser's own radio buttons, drawn as one row of segments. They give
 * what a choice of one in three needs: one stop for Tab, the arrow keys to move and
 * choose, and for a screen reader a group with a name, and which one is chosen. Where
 * the browser puts the keyboard on a radio button that is chosen, it stays there.
 *
 * The outline around the one the keyboard is on is drawn where the browser says the
 * keyboard is in use (`:focus-visible`), and also once an arrow key was pressed in the
 * group: WebKit doesn't say so of a radio button that an arrow key moved to (seen in
 * 27.2), and the outline went with the first arrow. And where a system's own few colours take
 * the place of the page's (`forced-colors`), the ground that sets the chosen one apart
 * is gone, so it is underlined there. */
export function ThemeChoice() {
  const [scheme, choose] = useScheme();
  // Whether an arrow key was the last thing used in the group, and not the mouse or a
  // finger. No other key: one held for something else, as for a shortcut, moves
  // nothing here.
  const [byKeys, setByKeys] = useState(false);
  return (
    <div className="shrink-0 border-t border-border px-5 py-3 text-xs">
      <fieldset
        data-keys={byKeys ? "" : undefined}
        onKeyDown={(event) => {
          if (event.key.startsWith("Arrow")) {
            setByKeys(true);
          }
        }}
        onPointerDown={() => {
          setByKeys(false);
        }}
      >
        <legend className="font-medium tracking-wide text-muted-foreground uppercase">
          Theme
        </legend>
        <div className="mt-2 grid grid-cols-3 gap-0.5 rounded-lg border border-input p-0.5">
          {CHOICES.map((choice) => (
            <label
              key={choice.scheme}
              className="relative cursor-pointer rounded-md px-2 py-1 text-center text-muted-foreground hover:text-foreground has-checked:bg-muted has-checked:font-medium has-checked:text-foreground in-data-keys:has-focus:outline-2 in-data-keys:has-focus:outline-offset-1 in-data-keys:has-focus:outline-ring has-focus-visible:outline-2 has-focus-visible:outline-offset-1 has-focus-visible:outline-ring forced-colors:has-checked:underline"
            >
              <input
                type="radio"
                name="theme"
                value={choice.scheme}
                checked={scheme === choice.scheme}
                onChange={() => {
                  choose(choice.scheme);
                }}
                className="sr-only"
              />
              {choice.label}
            </label>
          ))}
        </div>
      </fieldset>
    </div>
  );
}

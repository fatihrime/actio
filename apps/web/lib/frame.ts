// The page's frame: the header, and the place of the list of conversations beside the
// chat. The server sends it before anyone's session is known, and the browser paints
// it before any of the app's scripts have run. What arrives with the session fills
// places that were already there, so nothing on the page moves when it does (ADR 0015,
// revision of 2026-11-21).
//
// Two things about the frame the server can't know: whether this browser put the list
// away, and whether the reader chose a light or a dark page. Both are remembered in the
// browser's own storage. So the page's body begins with one small script, which runs
// while the HTML is read, before the frame's first paint: it marks the page's root
// element when the list was put away, and CSS then leaves the list no place
// (`list-away` in app/globals.css); and it marks it with the scheme that was chosen,
// which CSS then shows (`color-scheme` and the `dark` variant there; ADR 0015, revision
// "the reader chooses light or dark"). From then on the page keeps the list's mark in
// step with the switch itself, and changes the scheme's where the choice changes
// (lib/scheme.ts). Not in the page's head, where it held back the page's own scripts
// in Chromium until the stylesheet was in (ADR 0015, revision of 2026-11-22).
//
// This module holds what the script and the page must agree on. No hooks, and no
// "use client": the layout, which only the server renders, imports it.
import type { Scheme } from "./scheme";

/** Where this browser remembers that the list of conversations was put away: "on"
 * when it was (`useStoredSwitch`). People's browsers hold it under this name. */
export const LIST_HIDDEN_KEY = "actio.conversations.hidden";

/** The attribute on the page's root element that says the list was put away.
 * app/globals.css names it too, in the variant `list-away`. */
export const LIST_AWAY_MARK = "data-list-away";

/** Where this browser remembers the scheme the reader chose: "light" or "dark". As
 * the system is set, the default, is no entry (lib/scheme.ts). People's browsers hold
 * it under this name. */
export const SCHEME_KEY = "actio.scheme";

/** The attribute on the page's root element that says the scheme the reader chose:
 * "light" or "dark", and none while the page follows the system. app/globals.css
 * names it too. */
export const SCHEME_MARK = "data-scheme";

/** The script at the start of the page's body, as the browser runs it: it sets the
 * list's mark when the switch is on, and the scheme's when one of the two was chosen.
 * It never throws: storage can be off limits, as in some private windows, and then
 * the list keeps its place and the page follows the system. Its names are the block's
 * own, so it leaves nothing in the page's scope. */
export const FRAME_SCRIPT = `try{const s=localStorage,r=document.documentElement;if(s.getItem(${JSON.stringify(LIST_HIDDEN_KEY)})==="on")r.setAttribute(${JSON.stringify(LIST_AWAY_MARK)},"");const c=s.getItem(${JSON.stringify(SCHEME_KEY)});if(c==="light"||c==="dark")r.setAttribute(${JSON.stringify(SCHEME_MARK)},c)}catch(e){}`;

/** Set the mark on the page's root element, or take it away. */
export function markListAway(away: boolean): void {
  document.documentElement.toggleAttribute(LIST_AWAY_MARK, away);
}

/** Mark the page's root element with the scheme that was chosen, or take the mark
 * away for the system's. */
export function markScheme(scheme: Scheme): void {
  if (scheme === "system") {
    document.documentElement.removeAttribute(SCHEME_MARK);
  } else {
    document.documentElement.setAttribute(SCHEME_MARK, scheme);
  }
}

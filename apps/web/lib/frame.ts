// The page's frame: the header, and the place of the list of conversations beside the
// chat. The server sends it before anyone's session is known, and the browser paints
// it before any of the app's scripts have run. What arrives with the session fills
// places that were already there, so nothing on the page moves when it does (ADR 0015,
// revision of 2026-11-21).
//
// One thing about the frame the server can't know: whether this browser put the list
// away. That is remembered in the browser's own storage. So the page's head holds one
// small script, which runs while the HTML is read, before the first paint: it marks
// the page's root element when the list was put away, and CSS then leaves the list no
// place (`list-away` in app/globals.css). From then on the page keeps the mark in step
// with the switch itself.
//
// This module holds what the script and the page must agree on. No hooks, and no
// "use client": the layout, which only the server renders, imports it.

/** Where this browser remembers that the list of conversations was put away: "on"
 * when it was (`useStoredSwitch`). People's browsers hold it under this name. */
export const LIST_HIDDEN_KEY = "actio.conversations.hidden";

/** The attribute on the page's root element that says the list was put away.
 * app/globals.css names it too, in the variant `list-away`. */
export const LIST_AWAY_MARK = "data-list-away";

/** The script in the page's head, as the browser runs it: it sets the mark when the
 * switch is on. It never throws: storage can be off limits, as in some private
 * windows, and then the list keeps its place. */
export const LIST_AWAY_SCRIPT = `try{if(localStorage.getItem(${JSON.stringify(LIST_HIDDEN_KEY)})==="on")document.documentElement.setAttribute(${JSON.stringify(LIST_AWAY_MARK)},"")}catch(e){}`;

/** Set the mark on the page's root element, or take it away. */
export function markListAway(away: boolean): void {
  document.documentElement.toggleAttribute(LIST_AWAY_MARK, away);
}

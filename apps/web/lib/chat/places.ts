// Where the reader left each conversation, so that it opens there again.
//
// A place is a message, the block of it at the top of the reader's window, and how far
// below that block's top the window began: not how far down the conversation the
// reader was, which a window of another width, or messages that came meanwhile, would
// change. It is kept for one session (`Session.holder`): a new one finds none of the
// last one's. A reader who left a conversation at its end has no place there, and it
// opens at its end.
//
// The places are kept in the page's memory, which is the truth while the page lives,
// and for the tab, in its `sessionStorage`, written when the page is left (`pagehide`),
// not at every scroll, and read once, and again when the page comes back from the
// browser's back/forward cache: so a conversation opens at its place after the page
// is loaded again, in the same tab, for the same account. Another tab has its own.
// What is stored is ids, paths and offsets, nothing a message says, and nothing of it
// is believed without looking: what isn't as this module writes it is as if there
// were none, and so is storage the browser refuses.
import { accountOf } from "@/lib/session";

/** A place in a conversation.
 *
 * Measured from the block that was at the window's top, and not from its message's top:
 * what is above the window in that message may be another height when the conversation
 * is drawn anew, or grow after its first frame: a diagram, which is drawn later, or a
 * message shown as written until the code that draws markdown is there.
 *
 * A placeholder for code is what showed the message's top to be the wrong measure. The
 * markdown library draws each block of code with `content-visibility: auto`: off the
 * screen it was 218 px high until it had been near the window, and then its real
 * height. A message 984 px high when its reader left it 595 px in was 850 px high when
 * they came back, and the offset from its top put them 134 px from where they had
 * been, in Chromium, Firefox and WebKit. A rule of the stylesheet gives code its real
 * height from the first layout now (app/globals.css). */
export interface Spot {
  /** The id of the message at the top of what the reader saw: the first whose bottom
   * edge was below the top edge of the element that scrolls. */
  message: string;
  /** The way from that message's article down to the block that crossed that edge: a
   * child's index at each level, `PATH_LONGEST` at the most. Empty when no child of
   * the article did. */
  path: number[];
  /** How far the top edge of the element that scrolls was below that block's top
   * edge, in pixels. */
  offset: number;
}

/** How many conversations' places are kept at the most: those most recently left. */
export const PLACES_KEPT = 100;

/** How many levels below a message's article a place goes down at the most. */
export const PATH_LONGEST = 4;

/** Where the tab keeps the places: `{ account, places }`, the account whose they are,
 * and the places as pairs of a conversation's id and its place, the one left longest
 * ago first. */
export const PLACES_STORED = "actio.places";

/** Whose places are kept, and the places, by conversation, the one left longest ago
 * first. */
let holder: string | null = null;
const places = new Map<string, Spot>();
/** Whether the tab's copy has been read, which is once. */
let read = false;

/** Hold the places for the session `who`, as `Session.holder` says it. Says whether
 * there is one to hold them for.
 *
 * The first time there is someone, the places are the tab's copy, when it is their
 * account's, and the copy is let go otherwise. Before that, nobody's session is no
 * reason to let it go: the page knows nobody yet. After it, whenever `who` isn't whose
 * the places are, nobody's included, they are forgotten, and so is the tab's copy. */
function holdFor(who: string | null): boolean {
  if (!read) {
    if (who === null) {
      return false;
    }
    read = true;
    holder = who;
    const stored = fromTab();
    if (stored !== null && stored.account === accountOf(who)) {
      for (const [conversation, spot] of stored.places) {
        places.set(conversation, spot);
      }
    } else if (stored !== null) {
      forgetInTab();
    }
    window.addEventListener("pagehide", keepInTab);
    window.addEventListener("pageshow", backFromTheCache);
    return true;
  }
  if (who !== holder) {
    holder = who;
    places.clear();
    forgetInTab();
  }
  return who !== null;
}

/** Whose places to hold: the page says so whenever the session changes hands, so that
 * the last session's are let go at once, from the tab's copy too, and not when a
 * conversation next asks for a place. */
export function holdPlacesFor(who: string | null): void {
  holdFor(who);
}

/** Where the reader of session `who` left `conversation`, or nothing when they left it
 * at its end, or haven't left it in this session. */
export function placeLeft(
  who: string | null,
  conversation: string,
): Spot | null {
  return holdFor(who) ? (places.get(conversation) ?? null) : null;
}

/** Note where the reader of session `who` is in `conversation`: `spot`, or nothing at
 * its end. Noted last, it is the last to be forgotten. */
export function keepPlace(
  who: string | null,
  conversation: string,
  spot: Spot | null,
): void {
  if (!holdFor(who)) {
    return;
  }
  places.delete(conversation);
  if (spot === null) {
    return;
  }
  places.set(conversation, spot);
  for (const oldest of places.keys()) {
    if (places.size <= PLACES_KEPT) {
      break;
    }
    places.delete(oldest);
  }
}

/** The page is shown again. When it comes back from the browser's back/forward cache, a
 * newer page of the same tab may have written the tab's copy meanwhile: the places
 * become that copy, as far as it is this session's account's, and none otherwise, so
 * that this page's older ones aren't written over it when it is left again. The copy
 * itself stays as it is. */
function backFromTheCache(event: PageTransitionEvent): void {
  if (!event.persisted) {
    return;
  }
  places.clear();
  const stored = fromTab();
  if (
    stored !== null &&
    holder !== null &&
    stored.account === accountOf(holder)
  ) {
    for (const [conversation, spot] of stored.places) {
      places.set(conversation, spot);
    }
  }
}

/** Write the places to the tab's copy, as the page is left. */
function keepInTab(): void {
  if (holder === null || places.size === 0) {
    forgetInTab();
    return;
  }
  try {
    window.sessionStorage.setItem(
      PLACES_STORED,
      JSON.stringify({ account: accountOf(holder), places: [...places] }),
    );
  } catch {
    // The browser refuses its storage, or it is full: the places stay this page's.
  }
}

function forgetInTab(): void {
  try {
    window.sessionStorage.removeItem(PLACES_STORED);
  } catch {
    // Refused: nothing could have been written there either.
  }
}

/** The tab's copy, when there is one and it is as `keepInTab` writes it: nothing
 * otherwise. */
function fromTab(): { account: string; places: [string, Spot][] } | null {
  let value: unknown;
  try {
    const text = window.sessionStorage.getItem(PLACES_STORED);
    if (text === null) {
      return null;
    }
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (
    !isRecord(value) ||
    typeof value.account !== "string" ||
    !Array.isArray(value.places) ||
    value.places.length > PLACES_KEPT
  ) {
    return null;
  }
  const stored: [string, Spot][] = [];
  for (const entry of value.places as unknown[]) {
    if (!Array.isArray(entry) || entry.length !== 2) {
      return null;
    }
    const [conversation, spot] = entry as unknown[];
    if (typeof conversation !== "string" || !isSpot(spot)) {
      return null;
    }
    stored.push([
      conversation,
      { message: spot.message, path: [...spot.path], offset: spot.offset },
    ]);
  }
  return { account: value.account, places: stored };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Whether `value` is a place: a message's id, a short list of whole numbers, and an
 * offset that is a number. */
function isSpot(value: unknown): value is Spot {
  return (
    isRecord(value) &&
    typeof value.message === "string" &&
    Array.isArray(value.path) &&
    value.path.length <= PATH_LONGEST &&
    (value.path as unknown[]).every(
      (step) => typeof step === "number" && Number.isInteger(step) && step >= 0,
    ) &&
    typeof value.offset === "number" &&
    Number.isFinite(value.offset)
  );
}

// Where the reader left each conversation, so that it opens there again.
//
// A place is a message, the block of it at the top of the reader's window, and how far
// below that block's top the window began: not how far down the conversation the
// reader was, which a window of another width, or messages that came meanwhile, would
// change. It is kept in the page's memory and not in the browser's storage, so a reload
// opens a conversation at its end. It is kept for one session (`Session.holder`): a new
// one finds none of the last one's. A reader who left a conversation at its end has no
// place there, and it opens at its end.

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
   * child's index at each level. Empty when no child of the article did. */
  path: number[];
  /** How far the top edge of the element that scrolls was below that block's top
   * edge, in pixels. */
  offset: number;
}

/** How many conversations' places are kept at the most: those most recently left. */
export const PLACES_KEPT = 100;

/** Whose places are kept, and the places, by conversation, the one left longest ago
 * first. */
let holder: string | null = null;
const places = new Map<string, Spot>();

/** Hold places for `who`: when that isn't whose they are, those are forgotten. */
function holdFor(who: string | null): void {
  if (who !== holder) {
    holder = who;
    places.clear();
  }
}

/** Where the reader of session `who` left `conversation`, or nothing when they left it
 * at its end, or haven't left it in this session. */
export function placeLeft(
  who: string | null,
  conversation: string,
): Spot | null {
  holdFor(who);
  return places.get(conversation) ?? null;
}

/** Note where the reader of session `who` is in `conversation`: `spot`, or nothing at
 * its end. Noted last, it is the last to be forgotten. */
export function keepPlace(
  who: string | null,
  conversation: string,
  spot: Spot | null,
): void {
  holdFor(who);
  places.delete(conversation);
  if (who === null || spot === null) {
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

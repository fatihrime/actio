// Where the reader left each conversation: kept for one session, for a bounded number
// of conversations, none for a conversation left at its end, and for the tab, written
// when the page is left. What the page does with a place is the component tests'
// (components/chat/newest-first.test.tsx).
//
// Each test loads the module anew, as a page that was just opened, and a test of the
// tab's copy loads it anew again, as the page loaded again in the same tab.
import { beforeEach, expect, it, vi } from "vitest";

import type * as Places from "./places";

/** The module, as a page that was just opened has it. */
async function aPage(): Promise<typeof Places> {
  vi.resetModules();
  return import("./places");
}

/** The page is left: as it goes, it writes what it keeps for the tab. */
function leaveThePage(): void {
  window.dispatchEvent(new Event("pagehide"));
}

/** What the tab holds, as written. */
function inTheTab(): unknown {
  const text = window.sessionStorage.getItem("actio.places");
  return text === null ? null : (JSON.parse(text) as unknown);
}

// A session's holder is its account's id and how many sessions the page had before
// it (lib/session.ts).
const ADA = "ada/0";
const BEN = "ben/0";

const SPOT = { message: "m1", path: [0, 2], offset: 30 };
const ANOTHER = { message: "m2", path: [], offset: 12 };

let places: typeof Places;

beforeEach(async () => {
  places = await aPage();
});

it("keeps where the reader left each conversation", () => {
  places.keepPlace(ADA, "c1", SPOT);
  places.keepPlace(ADA, "c2", ANOTHER);

  expect(places.placeLeft(ADA, "c1")).toEqual(SPOT);
  expect(places.placeLeft(ADA, "c2")).toEqual(ANOTHER);
  expect(places.placeLeft(ADA, "c3")).toBeNull();
});

it("keeps the last place noted for a conversation", () => {
  places.keepPlace(ADA, "c1", SPOT);

  places.keepPlace(ADA, "c1", ANOTHER);

  expect(places.placeLeft(ADA, "c1")).toEqual(ANOTHER);
});

it("forgets a conversation's place once its reader is at its end", () => {
  places.keepPlace(ADA, "c1", SPOT);

  places.keepPlace(ADA, "c1", null);

  expect(places.placeLeft(ADA, "c1")).toBeNull();
});

it("has none of another session's places, and lets them go", () => {
  places.keepPlace(ADA, "c1", SPOT);

  // The same account, in the page's next session, has none either.
  expect(places.placeLeft("ada/1", "c1")).toBeNull();
  // Not kept beside the later session's, for a time it comes back: a session that
  // ended doesn't.
  expect(places.placeLeft(ADA, "c1")).toBeNull();
});

it("keeps nothing while nobody has a session", () => {
  places.keepPlace(null, "c1", SPOT);

  expect(places.placeLeft(null, "c1")).toBeNull();
});

it("keeps the places of the conversations most recently left, and forgets the oldest first", () => {
  expect(places.PLACES_KEPT).toBe(100);
  for (let left = 0; left < places.PLACES_KEPT; left++) {
    places.keepPlace(ADA, `c${String(left)}`, SPOT);
  }
  // Left again, the first is the most recent now.
  places.keepPlace(ADA, "c0", ANOTHER);

  places.keepPlace(ADA, "one more", SPOT);

  expect(places.placeLeft(ADA, "c0")).toEqual(ANOTHER);
  expect(places.placeLeft(ADA, "c1")).toBeNull();
  expect(places.placeLeft(ADA, "c2")).toEqual(SPOT);
  expect(places.placeLeft(ADA, `c${String(places.PLACES_KEPT - 1)}`)).toEqual(
    SPOT,
  );
  expect(places.placeLeft(ADA, "one more")).toEqual(SPOT);
});

it("writes the places for the tab when the page is left, and not before", () => {
  places.keepPlace(ADA, "c1", SPOT);
  places.keepPlace(ADA, "c2", ANOTHER);
  expect(inTheTab()).toBeNull();

  leaveThePage();

  // Whose they are, and the places, the one left longest ago first: ids, paths and
  // offsets alone.
  expect(inTheTab()).toEqual({
    account: "ada",
    places: [
      ["c1", SPOT],
      ["c2", ANOTHER],
    ],
  });
});

it.each([
  { how: "", before: ADA },
  // The page counted its sessions from 0 again when it was loaded again.
  { how: ", whichever of that page's sessions it was", before: "ada/3" },
])(
  "has the places again when the page is loaded again in the same tab, for the same account$how",
  async ({ before }) => {
    places.keepPlace(before, "c1", SPOT);
    places.keepPlace(before, "c2", ANOTHER);
    places.keepPlace(before, "c2", null); // left at its end
    leaveThePage();

    const again = await aPage();

    expect(again.placeLeft(ADA, "c1")).toEqual(SPOT);
    expect(again.placeLeft(ADA, "c2")).toBeNull();
  },
);

it("has none of another account's places when the page is loaded again, and lets the tab's go", async () => {
  places.keepPlace(ADA, "c1", SPOT);
  leaveThePage();

  const again = await aPage();

  expect(again.placeLeft(BEN, "c1")).toBeNull();
  expect(inTheTab()).toBeNull();
  // And not for the first account after that, either.
  const later = await aPage();
  expect(later.placeLeft(ADA, "c1")).toBeNull();
});

it.each([
  { how: "to another account", to: BEN },
  { how: "to nobody, as the session ends", to: null },
])(
  "lets the tab's places go as soon as the page says the session changed hands $how, before any place is asked for",
  ({ to }) => {
    places.keepPlace(ADA, "c1", SPOT);
    leaveThePage();
    expect(inTheTab()).not.toBeNull();

    places.holdPlacesFor(to);

    expect(inTheTab()).toBeNull();
    expect(places.placeLeft(ADA, "c1")).toBeNull();
  },
);

it("keeps the tab's places while nobody has a session yet, for the account that comes", async () => {
  places.keepPlace(ADA, "c1", SPOT);
  leaveThePage();
  const again = await aPage();

  // The page doesn't know whose session it has yet.
  again.holdPlacesFor(null);
  expect(again.placeLeft(null, "c1")).toBeNull();
  again.keepPlace(null, "c2", ANOTHER);

  expect(inTheTab()).toEqual({ account: "ada", places: [["c1", SPOT]] });
  again.holdPlacesFor(ADA);
  expect(again.placeLeft(ADA, "c1")).toEqual(SPOT);
  expect(again.placeLeft(ADA, "c2")).toBeNull();
});

it("takes the tab's places again when the page comes back from the browser's cache, and writes those when it is left", () => {
  places.keepPlace(ADA, "c1", SPOT);
  leaveThePage();
  // A newer page of the same tab, which the browser left this one for, wrote its own.
  window.sessionStorage.setItem(
    "actio.places",
    JSON.stringify({ account: "ada", places: [["c2", ANOTHER]] }),
  );

  window.dispatchEvent(
    new PageTransitionEvent("pageshow", { persisted: true }),
  );

  expect(places.placeLeft(ADA, "c1")).toBeNull();
  expect(places.placeLeft(ADA, "c2")).toEqual(ANOTHER);
  leaveThePage();
  expect(inTheTab()).toEqual({ account: "ada", places: [["c2", ANOTHER]] });
});

it("has none of another account's places when the page comes back from the browser's cache, and leaves the tab's alone", () => {
  places.keepPlace(ADA, "c1", SPOT);
  leaveThePage();
  const newer = { account: "ben", places: [["c2", ANOTHER]] };
  window.sessionStorage.setItem("actio.places", JSON.stringify(newer));

  window.dispatchEvent(
    new PageTransitionEvent("pageshow", { persisted: true }),
  );

  expect(places.placeLeft(ADA, "c1")).toBeNull();
  expect(places.placeLeft(ADA, "c2")).toBeNull();
  expect(inTheTab()).toEqual(newer);
});

it("keeps its places when the page is shown, not from the browser's cache", () => {
  places.keepPlace(ADA, "c1", SPOT);
  leaveThePage();
  window.sessionStorage.setItem(
    "actio.places",
    JSON.stringify({ account: "ada", places: [["c2", ANOTHER]] }),
  );

  window.dispatchEvent(
    new PageTransitionEvent("pageshow", { persisted: false }),
  );

  expect(places.placeLeft(ADA, "c1")).toEqual(SPOT);
  expect(places.placeLeft(ADA, "c2")).toBeNull();
});

it.each<{ what: string; stored: string }>([
  { what: "isn't JSON", stored: "{places" },
  { what: "is a list", stored: "[]" },
  {
    what: "names no account",
    stored: JSON.stringify({ places: [["c1", SPOT]] }),
  },
  {
    what: "holds no list of places",
    stored: JSON.stringify({ account: "ada", places: { c1: SPOT } }),
  },
  {
    what: "holds an entry that is no pair",
    stored: JSON.stringify({ account: "ada", places: [["c1", SPOT, 1]] }),
  },
  {
    what: "names a conversation by something other than text",
    stored: JSON.stringify({ account: "ada", places: [[1, SPOT]] }),
  },
  {
    what: "names a message by something other than text",
    stored: JSON.stringify({
      account: "ada",
      places: [["c1", { ...SPOT, message: 7 }]],
    }),
  },
  {
    what: "has a path with a part of a number",
    stored: JSON.stringify({
      account: "ada",
      places: [["c1", { ...SPOT, path: [0, 1.5] }]],
    }),
  },
  {
    what: "has a path with a number below nought",
    stored: JSON.stringify({
      account: "ada",
      places: [["c1", { ...SPOT, path: [-1] }]],
    }),
  },
  {
    what: "has a path too long",
    stored: JSON.stringify({
      account: "ada",
      places: [["c1", { ...SPOT, path: [0, 0, 0, 0, 0] }]],
    }),
  },
  {
    what: "has a path that isn't a list",
    stored: JSON.stringify({
      account: "ada",
      places: [["c1", { ...SPOT, path: "0,2" }]],
    }),
  },
  {
    what: "has an offset that isn't a number",
    stored: JSON.stringify({
      account: "ada",
      places: [["c1", { ...SPOT, offset: "30" }]],
    }),
  },
  {
    // JSON has no infinity: one written there reads back as nothing.
    what: "has an offset of no number at all",
    stored: JSON.stringify({
      account: "ada",
      places: [["c1", { ...SPOT, offset: Infinity }]],
    }),
  },
  {
    // Written by hand, a number too large to be one is read back as infinity.
    what: "has an offset too large to be a number",
    stored: JSON.stringify({
      account: "ada",
      places: [["c1", SPOT]],
    }).replace('"offset":30', '"offset":1e999'),
  },
  {
    what: "holds more places than are kept",
    stored: JSON.stringify({
      account: "ada",
      places: Array.from({ length: 101 }, (_, index) => [
        `c${String(index)}`,
        SPOT,
      ]),
    }),
  },
])("takes what the tab holds for none when it $what", async ({ stored }) => {
  window.sessionStorage.setItem("actio.places", stored);

  const again = await aPage();

  expect(again.placeLeft(ADA, "c1")).toBeNull();
  // And keeps on as ever.
  again.keepPlace(ADA, "c2", ANOTHER);
  expect(again.placeLeft(ADA, "c2")).toEqual(ANOTHER);
});

it("keeps the places of the page alone when the browser refuses its storage", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new DOMException("Refused", "SecurityError");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("Full", "QuotaExceededError");
  });

  places.keepPlace(ADA, "c1", SPOT);
  leaveThePage();

  expect(places.placeLeft(ADA, "c1")).toEqual(SPOT);
});

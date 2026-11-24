// Where the reader left each conversation: kept for one session, for a bounded number
// of conversations, and none for a conversation left at its end. What the page does
// with a place is the component tests' (components/chat/newest-first.test.tsx).
import { expect, it } from "vitest";

import { keepPlace, placeLeft, PLACES_KEPT } from "./places";

// The module keeps its places for as long as the page is open, here for the whole
// file: each test is a session of its own, which finds none of another's.
let sessions = 0;
function aSession(): string {
  sessions += 1;
  return `session ${String(sessions)}`;
}

const SPOT = { message: "m1", path: [0, 2], offset: 30 };
const ANOTHER = { message: "m2", path: [], offset: 12 };

it("keeps where the reader left each conversation", () => {
  const who = aSession();

  keepPlace(who, "c1", SPOT);
  keepPlace(who, "c2", ANOTHER);

  expect(placeLeft(who, "c1")).toEqual(SPOT);
  expect(placeLeft(who, "c2")).toEqual(ANOTHER);
  expect(placeLeft(who, "c3")).toBeNull();
});

it("keeps the last place noted for a conversation", () => {
  const who = aSession();
  keepPlace(who, "c1", SPOT);

  keepPlace(who, "c1", ANOTHER);

  expect(placeLeft(who, "c1")).toEqual(ANOTHER);
});

it("forgets a conversation's place once its reader is at its end", () => {
  const who = aSession();
  keepPlace(who, "c1", SPOT);

  keepPlace(who, "c1", null);

  expect(placeLeft(who, "c1")).toBeNull();
});

it("has none of another session's places, and lets them go", () => {
  const earlier = aSession();
  keepPlace(earlier, "c1", SPOT);
  const later = aSession();

  expect(placeLeft(later, "c1")).toBeNull();
  // Not kept beside the later session's, for a time it comes back: a session that
  // ended doesn't.
  expect(placeLeft(earlier, "c1")).toBeNull();
});

it("keeps nothing while nobody has a session", () => {
  keepPlace(null, "c1", SPOT);

  expect(placeLeft(null, "c1")).toBeNull();
});

it("keeps the places of the conversations most recently left, and forgets the oldest first", () => {
  expect(PLACES_KEPT).toBe(100);
  const who = aSession();
  for (let left = 0; left < PLACES_KEPT; left++) {
    keepPlace(who, `c${String(left)}`, SPOT);
  }
  // Left again, the first is the most recent now.
  keepPlace(who, "c0", ANOTHER);

  keepPlace(who, "one more", SPOT);

  expect(placeLeft(who, "c0")).toEqual(ANOTHER);
  expect(placeLeft(who, "c1")).toBeNull();
  expect(placeLeft(who, "c2")).toEqual(SPOT);
  expect(placeLeft(who, `c${String(PLACES_KEPT - 1)}`)).toEqual(SPOT);
  expect(placeLeft(who, "one more")).toEqual(SPOT);
});

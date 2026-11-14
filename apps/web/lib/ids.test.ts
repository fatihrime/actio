import { describe, expect, it, vi } from "vitest";

import { isId, newId } from "./ids";

describe("newId", () => {
  it("makes a version 7 UUID", () => {
    expect(newId()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it("begins with the time, so later ids sort after earlier ones", () => {
    const earlier = newId(Date.UTC(2026, 10, 10, 12, 0, 0));
    const later = newId(Date.UTC(2026, 10, 10, 12, 0, 1));

    // 2026-11-10T12:00:00Z is 0x01a1c5545200 milliseconds after 1970.
    expect(earlier.slice(0, 13)).toBe("01a1c554-5200");
    expect(later > earlier).toBe(true);
  });

  it("never makes the same id twice, even in the same millisecond", () => {
    const moment = Date.now();
    const ids = new Set(Array.from({ length: 1000 }, () => newId(moment)));

    expect(ids.size).toBe(1000);
  });

  it("works on a page served over plain HTTP, where randomUUID is missing", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => bytes.fill(0xff),
    });

    expect(newId(0)).toBe("00000000-0000-7fff-bfff-ffffffffffff");
  });
});

describe("isId", () => {
  it("tells a UUID from other text", () => {
    expect(isId(newId())).toBe(true);
    expect(isId("01900000-0000-7000-8000-0000000000C1")).toBe(true);
    expect(isId("new")).toBe(false);
    expect(isId("01900000-0000-7000-8000-0000000000c1/..")).toBe(false);
  });
});

import { describe, expect, it } from "vitest";

import { conversationIn, conversationPath, go, placeIn } from "./location";

const ID = "01900000-0000-7000-8000-0000000000c1";

describe("placeIn", () => {
  it("knows the three places for the session", () => {
    expect(placeIn("/login")).toEqual({ kind: "login" });
    expect(placeIn("/signup")).toEqual({ kind: "signup" });
    expect(placeIn("/account")).toEqual({ kind: "account" });
    expect(placeIn("/login/")).toEqual({ kind: "login" });
  });

  it("takes everything else for the chat, with the conversation it names", () => {
    expect(placeIn("/")).toEqual({ kind: "chat", conversation: null });
    expect(placeIn(`/c/${ID}`)).toEqual({ kind: "chat", conversation: ID });
    expect(placeIn("/login/more")).toEqual({
      kind: "chat",
      conversation: null,
    });
  });
});

describe("conversationIn", () => {
  it("reads the conversation a path names", () => {
    expect(conversationIn(`/c/${ID}`)).toBe(ID);
    expect(conversationIn(`/c/${ID}/`)).toBe(ID);
    expect(conversationIn(conversationPath(ID))).toBe(ID);
  });

  it("reads an id the same however it is capitalized", () => {
    expect(conversationIn(`/c/${ID.toUpperCase()}`)).toBe(ID);
  });

  it("finds none in a path that names no conversation", () => {
    expect(conversationIn("/")).toBeNull();
    expect(conversationIn("/c/")).toBeNull();
    expect(conversationIn("/c/not-an-id")).toBeNull();
    expect(conversationIn(`/c/${ID}/more`)).toBeNull();
    expect(conversationIn(`/x/${ID}`)).toBeNull();
  });
});

describe("go", () => {
  it("adds a step to the browser's history, or replaces the current one", () => {
    const steps = window.history.length;

    go(`/c/${ID}`);

    expect(window.location.pathname).toBe(`/c/${ID}`);
    expect(window.history.length).toBe(steps + 1);

    go("/", { replace: true });

    expect(window.location.pathname).toBe("/");
    expect(window.history.length).toBe(steps + 1);
  });

  it("adds no step for the address the page is already at", () => {
    const steps = window.history.length;

    go(window.location.pathname);

    expect(window.history.length).toBe(steps);
  });
});

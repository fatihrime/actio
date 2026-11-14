// What the address bar names, and how the page changes it.
//
// "/" is a new conversation; "/c/<id>" is a stored one. "/login", "/signup" and
// "/account" are the three places for whose session this is. Moving between any of
// them never loads a new page: the app changes the address through the browser's
// history, and reads it back from there, so Back and Forward work and a reload shows
// the same place. Next.js keeps its router in step with such changes by itself.
import { useSyncExternalStore } from "react";

import { isId } from "./ids";

const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  window.addEventListener("popstate", listener); // Back and Forward
  return () => {
    listeners.delete(listener);
    window.removeEventListener("popstate", listener);
  };
}

/** The path in the address bar, kept current. */
export function usePath(): string {
  return useSyncExternalStore(
    subscribe,
    () => window.location.pathname,
    // While the server prepares the page there is no address bar. Nothing depends
    // on it there: a conversation is only shown in the browser, once it has a session.
    () => "/",
  );
}

/** Change the address without loading a page. `replace` leaves no step to go back to. */
export function go(path: string, { replace = false } = {}): void {
  if (window.location.pathname === path) {
    return;
  }
  if (replace) {
    window.history.replaceState(null, "", path);
  } else {
    window.history.pushState(null, "", path);
  }
  for (const listener of listeners) {
    listener();
  }
}

/** The address of a conversation. */
export function conversationPath(id: string): string {
  return `/c/${id}`;
}

/** The conversation a path names, if it names one. */
export function conversationIn(path: string): string | null {
  const id = /^\/c\/([^/]+)\/?$/.exec(path)?.[1];
  return id !== undefined && isId(id) ? id.toLowerCase() : null;
}

/** Where to log in, to sign up, and to see to an account. */
export const LOGIN = "/login";
export const SIGNUP = "/signup";
export const ACCOUNT = "/account";

/** What the app shows at an address: the chat, with a conversation or a new one, or
 * one of the places for the session. */
export type Place =
  | { kind: "chat"; conversation: string | null }
  | { kind: "login" | "signup" | "account" };

/** The place a path names. Whatever names nothing else is the chat. */
export function placeIn(path: string): Place {
  switch (path.replace(/\/$/, "")) {
    case LOGIN:
      return { kind: "login" };
    case SIGNUP:
      return { kind: "signup" };
    case ACCOUNT:
      return { kind: "account" };
    default:
      return { kind: "chat", conversation: conversationIn(path) };
  }
}

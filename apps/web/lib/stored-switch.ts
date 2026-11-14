// A choice between two that this browser remembers: reasoning on or off, the list of
// conversations shown or hidden. Kept in the browser's own storage, so it is the
// browser's and not the account's.
import { useCallback, useState } from "react";

/** A switch that is remembered in this browser, off until someone turns it on. */
export function useStoredSwitch(key: string): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(() => {
    try {
      return window.localStorage.getItem(key) === "on";
    } catch {
      return false; // storage is off limits, as in some private windows
    }
  });
  const set = useCallback(
    (next: boolean) => {
      setOn(next);
      try {
        window.localStorage.setItem(key, next ? "on" : "off");
      } catch {
        // Not remembered, then.
      }
    },
    [key],
  );
  return [on, set];
}

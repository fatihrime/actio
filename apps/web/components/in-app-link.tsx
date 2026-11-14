"use client";

import type { AnchorHTMLAttributes, MouseEvent } from "react";

import { go } from "@/lib/location";

/** A link to another place in the app.
 *
 * A plain click changes the address without loading a page (`lib/location.ts`): a
 * page load would end an answer that is being written. It is a real link all the
 * same: a click with a modifier key, or with another button, is left to the browser,
 * which opens the address in a new tab or window.
 */
export function InAppLink({
  href,
  replace = false,
  onFollow,
  children,
  ...rest
}: {
  href: string;
  /** Leave no step to go back to: for a move between two forms of one thing. */
  replace?: boolean;
  /** Called after a click that the app took for itself. */
  onFollow?: () => void;
} & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "onClick">) {
  function follow(event: MouseEvent) {
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    go(href, { replace });
    onFollow?.();
  }

  return (
    <a href={href} onClick={follow} {...rest}>
      {children}
    </a>
  );
}

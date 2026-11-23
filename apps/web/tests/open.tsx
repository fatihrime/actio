// How a component test starts: the whole app, at an address, in a browser with
// nothing remembered, and a person in front of it.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UserEvent } from "@testing-library/user-event";
import { StrictMode, Suspense } from "react";
import type { ReactElement } from "react";
import { SWRConfig } from "swr";
import { vi } from "vitest";

import { ChatApp } from "@/components/chat/chat-app";
import { FRAME_SCRIPT } from "@/lib/frame";

/** The whole app, as each of its pages shows it, with a cache of its own: what one
 * test read from the API isn't the next one's. */
export function app(): ReactElement {
  return (
    <SWRConfig value={{ provider: () => new Map() }}>
      <ChatApp />
    </SWRConfig>
  );
}

/** What stands in the page's place when something on it waits outside a transition,
 * under `open(path, { suspense: true })`. */
export const SUSPENDED = "The page is waiting.";

/** Open the app at `path`, and hand back the person who uses it. Set up the fake API
 * first (./fake-api.ts): the page asks it at once.
 *
 * `strict` renders it in React's StrictMode, as Next.js does in development
 * (`make dev`): each component's effects are run, undone and run again when it
 * appears.
 *
 * `suspense` renders it inside a Suspense boundary. When something on the page waits
 * for a promise (`use`), the boundary puts `SUSPENDED` in the page's place, unless the
 * update that drew it was a transition: React then leaves the page as it was until
 * the promise is kept. Without a boundary, it leaves the page as it was either way. */
export function open(
  path = "/",
  { strict = false, suspense = false } = {},
): UserEvent {
  window.history.replaceState(null, "", path);
  const page = suspense ? (
    <Suspense fallback={<p>{SUSPENDED}</p>}>{app()}</Suspense>
  ) : (
    app()
  );
  render(strict ? <StrictMode>{page}</StrictMode> : page);
  return userEvent.setup();
}

/** What the browser does before a page's first paint: run the script at the start of
 * its body, which marks the page's root by what this browser remembers (lib/frame.ts).
 * `open` doesn't, so that a test of what the page itself does with the marks starts
 * without them. */
export function runFrameScript(): void {
  const script = document.createElement("script");
  script.textContent = FRAME_SCRIPT;
  document.body.prepend(script);
  script.remove();
}

/** Type a question into the box, and send it. */
export async function ask(user: UserEvent, text: string): Promise<void> {
  await user.type(await screen.findByLabelText("Message"), text);
  await user.click(screen.getByRole("button", { name: "Send" }));
}

/** A window too narrow for the list to stand beside the chat. */
export function narrowWindow(): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: false,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

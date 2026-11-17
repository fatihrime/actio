// How a component test starts: the whole app, at an address, in a browser with
// nothing remembered, and a person in front of it.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UserEvent } from "@testing-library/user-event";
import { StrictMode } from "react";
import { SWRConfig } from "swr";

import { ChatApp } from "@/components/chat/chat-app";

/** Open the app at `path`, and hand back the person who uses it. Set up the fake API
 * first (./fake-api.ts): the page asks it at once.
 *
 * `strict` renders it in React's StrictMode, as Next.js does in development
 * (`make dev`): each component's effects are run, undone and run again when it
 * appears. */
export function open(path = "/", { strict = false } = {}): UserEvent {
  window.history.replaceState(null, "", path);
  const app = (
    // Its own cache, so what one test read from the API isn't the next one's.
    <SWRConfig value={{ provider: () => new Map() }}>
      <ChatApp />
    </SWRConfig>
  );
  render(strict ? <StrictMode>{app}</StrictMode> : app);
  return userEvent.setup();
}

/** Type a question into the box, and send it. */
export async function ask(user: UserEvent, text: string): Promise<void> {
  await user.type(await screen.findByLabelText("Message"), text);
  await user.click(screen.getByRole("button", { name: "Send" }));
}

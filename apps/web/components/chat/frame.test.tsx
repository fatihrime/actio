// The page's frame as the browser takes it over from the server: React hydrates the
// HTML that was sent, and doesn't repair an attribute that differs from what the
// browser's first render makes, nor what CSS was told by it. So nothing rendered
// before the session is known may depend on what only the browser knows: the switch
// that put the list away, or the window's width (ADR 0015, revision of 2026-11-21).
//
// What these tests can see is a frame that depends on the switch: it is read at that
// first render, and then differs from what was sent. The window's width is read with
// the server's answer first and the browser's right after (`useMedia`), so it never
// differs here: a frame that depends on it changes a moment after the first paint,
// which only a real browser shows (e2e/frame.spec.ts). The narrow window's visitor
// holds the rest: the frame is taken over as it was sent, and the session then fills
// it as a narrow window wants.
import { act, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { hydrateRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { describe, expect, it, onTestFinished, vi } from "vitest";

import { LIST_HIDDEN_KEY } from "@/lib/frame";
import { fakeApi, GUEST, json, later } from "@/tests/fake-api";
import { app, narrowWindow } from "@/tests/open";

/** Hydrate `html`, made by the server, with `node`, as the browser does, and hand
 * back what React had to say about it: the mismatches it recovered from, and
 * whatever it wrote to the console as an error meanwhile. */
async function hydrate(html: string, node: ReactElement): Promise<unknown[]> {
  const said: unknown[] = [];
  const errors = vi
    .spyOn(console, "error")
    .mockImplementation((...args: unknown[]) => {
      said.push(args);
    });
  const container = document.createElement("div");
  container.innerHTML = html;
  document.body.append(container);
  const root = await act(() =>
    hydrateRoot(container, node, {
      onRecoverableError: (error) => {
        said.push(error);
      },
    }),
  );
  errors.mockRestore();
  onTestFinished(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });
  return said;
}

describe("the helper that hydrates", () => {
  // Without these, the tests below could pass one day without proving anything.
  it.each([
    ["a text", <p key="server">server</p>, <p key="client">browser</p>],
    [
      "an attribute",
      <p key="server" title="server">
        Same
      </p>,
      <p key="client" title="browser">
        Same
      </p>,
    ],
  ])(
    "says so when %s differs between the two sides",
    async (_, server, browser) => {
      expect(await hydrate(renderToString(server), browser)).not.toEqual([]);
    },
  );
});

describe("the page's frame", () => {
  it.each([
    {
      visitor: "nothing remembered",
      before: () => undefined,
      expanded: "true",
      listed: true,
    },
    {
      visitor: "the list put away on an earlier visit",
      before: () => {
        window.localStorage.setItem(LIST_HIDDEN_KEY, "on");
      },
      expanded: "false",
      listed: false,
    },
    {
      visitor: "a narrow window",
      before: narrowWindow,
      expanded: "false",
      listed: false,
    },
  ])(
    "is taken over from the server as it was sent, with $visitor",
    async ({ before, expanded, listed }) => {
      // As the server makes it, which knows nothing of the browser. In the simulated
      // one the page can read the browser's storage while it is made, so this comes
      // before the browser remembers anything.
      const html = renderToString(app());
      before();
      const session = later(() => json(201, GUEST));
      fakeApi().on("POST /api/session/guest", session.answer);

      expect(await hydrate(html, app())).toEqual([]);
      expect(screen.getByText("Starting…")).toBeInTheDocument();

      session.release();

      // Then the session fills the places it had.
      expect(
        await screen.findByRole("button", { name: "Conversations" }),
      ).toHaveAttribute("aria-expanded", expanded);
      if (listed) {
        expect(
          await screen.findByText(/No conversations yet/),
        ).toBeInTheDocument();
      } else {
        expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
      }
    },
  );
});

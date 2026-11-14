"use client";

import { useEffect, useRef, useState } from "react";

import { Notice, NoticeButton } from "@/components/notice";
import { ApiProblem } from "@/lib/api/client";
import type { Conversations } from "@/lib/chat/list";
import { explain } from "@/lib/chat/outcomes";
import { conversationPath } from "@/lib/location";

import { Row } from "./row";

/** The list of the caller's conversations, most recently active first: open one,
 * rename it, delete it.
 *
 * The list is read by the page around it, which also knows when to read it again
 * (`lib/chat/list.ts`). What is shown here is always what the server said last.
 */
export function Sidebar({
  list,
  current,
  takesFocus = false,
  onOpen,
  onDeleted,
}: {
  list: Conversations;
  /** The conversation on the page, if the server has it. */
  current: string | null;
  /** Whether the keyboard goes to the list when it appears: when it was just asked
   * for, and lies over everything else. */
  takesFocus?: boolean;
  onOpen: (id: string) => void;
  /** Called after a conversation was deleted, with its id. */
  onDeleted: (id: string) => void;
}) {
  // What went wrong with the last rename or delete. A failure to read the list is the
  // list's own (`list.error`).
  const [problem, setProblem] = useState<unknown>(null);
  const region = useRef<HTMLElement>(null);
  const { items } = list;

  useEffect(() => {
    if (takesFocus) {
      region.current?.focus();
    }
  }, [takesFocus]);

  async function rename(id: string, title: string): Promise<boolean> {
    setProblem(null);
    try {
      await list.rename(id, title);
      return true;
    } catch (error) {
      setProblem(error);
      if (error instanceof ApiProblem && error.status === 404) {
        list.refresh(); // deleted meanwhile, in another window
      }
      return false;
    }
  }

  async function remove(id: string): Promise<boolean> {
    setProblem(null);
    // Where the keyboard goes when the row it is on is gone: the row that takes its
    // place, the one before it, or the list as a whole.
    const at = items?.findIndex((item) => item.id === id) ?? -1;
    const neighbour = items?.[at + 1] ?? items?.[at - 1];
    try {
      await list.remove(id);
    } catch (error) {
      setProblem(error);
      return false;
    }
    const link =
      neighbour === undefined
        ? null
        : region.current?.querySelector<HTMLElement>(
            `a[href="${conversationPath(neighbour.id)}"]`,
          );
    (link ?? region.current)?.focus();
    onDeleted(id);
    return true;
  }

  return (
    <nav
      ref={region}
      aria-labelledby="conversations-heading"
      tabIndex={-1}
      className="flex min-h-0 flex-1 flex-col outline-none"
    >
      <h2
        id="conversations-heading"
        className="shrink-0 px-5 pt-4 pb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase"
      >
        Conversations
      </h2>
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2 pb-3">
        {problem !== null && (
          <Notice
            tone="warning"
            actions={
              <NoticeButton
                onClick={() => {
                  setProblem(null);
                }}
              >
                Dismiss
              </NoticeButton>
            }
          >
            {explain(problem)}
          </Notice>
        )}
        {list.error !== null && (
          <Notice
            tone="warning"
            actions={
              <NoticeButton onClick={list.refresh}>Try again</NoticeButton>
            }
          >
            <p className="font-medium">The list couldn’t be read.</p>
            <p>{explain(list.error)}</p>
          </Notice>
        )}
        {items === null ? (
          list.error === null && (
            <p
              role="status"
              className="px-3 py-2 text-sm text-muted-foreground"
            >
              Loading…
            </p>
          )
        ) : items.length === 0 ? (
          <p className="px-3 py-2 text-sm text-muted-foreground">
            No conversations yet. What you ask is kept here.
          </p>
        ) : (
          <ul className="space-y-0.5">
            {items.map((item) => (
              <Row
                key={item.id}
                conversation={item}
                current={item.id === current}
                onOpen={() => {
                  onOpen(item.id);
                }}
                onRename={(title) => rename(item.id, title)}
                onDelete={() => remove(item.id)}
              />
            ))}
          </ul>
        )}
        {list.more && (
          <button
            type="button"
            onClick={list.showMore}
            disabled={list.readingMore}
            className="w-full rounded-lg px-3 py-2 text-left text-sm text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring disabled:opacity-60"
          >
            {list.readingMore ? "Loading…" : "Show older conversations"}
          </button>
        )}
      </div>
    </nav>
  );
}

// The list of the caller's conversations: read a page at a time, most recently active
// first, and kept in step with what the page does to them.
//
// The server is the truth about the list, as about a conversation. After a turn the
// list is read again, instead of being rearranged here: the server decides the order.
// A rename or a delete changes what is held only once the server has done it.
import { useCallback, useMemo } from "react";
import useSWRInfinite from "swr/infinite";

import {
  ApiProblem,
  deleteConversation,
  listConversations,
  renameConversation,
} from "@/lib/api/client";
import type { ConversationList, ConversationSummary } from "@/lib/api/client";

/** A page of the list is held under whose list it is, and where the page before it
 * ended. So a list never outlives its owner's session, and the pages follow from each
 * other: when the first one changes, the ones after it are read from its new end. */
type Page = readonly [
  what: "conversations",
  owner: string,
  after: string | null,
];

export interface Conversations {
  /** The conversations read so far. Nothing until the first page has arrived. */
  items: ConversationSummary[] | null;
  /** What went wrong the last time the list was read, if anything did. */
  error: unknown;
  /** Whether there are older conversations that haven't been read yet. */
  more: boolean;
  /** Whether older ones are being read right now. */
  readingMore: boolean;
  /** Read the next page of older conversations. */
  showMore: () => void;
  /** Read the list again, as far as it was read. */
  refresh: () => void;
  /** Give a conversation a title. Throws what the API refused it with. */
  rename: (id: string, title: string) => Promise<void>;
  /** Delete a conversation. One that is already gone counts as deleted. Throws what
   * the API refused it with. */
  remove: (id: string) => Promise<void>;
}

/** The conversations of `owner`, an account's id. Nothing is read while there is no
 * owner: before the session is known. */
export function useConversations(owner: string | null): Conversations {
  const { data, error, size, setSize, mutate, isValidating } = useSWRInfinite<
    ConversationList,
    unknown
  >(
    (index: number, before: ConversationList | null): Page | null => {
      if (owner === null) {
        return null;
      }
      if (index === 0) {
        return ["conversations", owner, null];
      }
      const after = before?.next_cursor ?? null;
      return after === null ? null : ["conversations", owner, after];
    },
    ([, , after]: Page) => listConversations(after),
    // A failure is shown, with a button. The list is also read again whenever the
    // window is returned to, which is retry enough.
    { shouldRetryOnError: false },
  );

  const items = useMemo(() => {
    if (data === undefined) {
      return null;
    }
    // Pages are read one after another, and a conversation can move between two
    // readings. Shown twice, it would be two rows under one name.
    const seen = new Set<string>();
    const once: ConversationSummary[] = [];
    for (const item of data.flatMap((page) => page.items)) {
      if (!seen.has(item.id)) {
        seen.add(item.id);
        once.push(item);
      }
    }
    return once;
  }, [data]);

  const read = data?.length ?? 0;

  const showMore = useCallback(() => {
    // When the page asked for last time never arrived, ask for it again; one more
    // page otherwise.
    void (size > read ? mutate() : setSize(read + 1));
  }, [mutate, read, setSize, size]);

  const refresh = useCallback(() => {
    void mutate();
  }, [mutate]);

  const rename = useCallback(
    async (id: string, title: string) => {
      const renamed = await renameConversation(id, title);
      await mutate(
        (pages) => each(pages, (item) => (item.id === id ? renamed : item)),
        {
          revalidate: false,
        },
      );
    },
    [mutate],
  );

  const remove = useCallback(
    async (id: string) => {
      try {
        await deleteConversation(id);
      } catch (error) {
        if (!(error instanceof ApiProblem && error.status === 404)) {
          throw error;
        }
        // Deleted already, in another window. Gone is what was asked for.
      }
      await mutate(
        (pages) => each(pages, (item) => (item.id === id ? null : item)),
        {
          revalidate: false,
        },
      );
    },
    [mutate],
  );

  return {
    items,
    error: error ?? null,
    more: data?.at(-1)?.next_cursor != null,
    readingMore: isValidating && size > read && read > 0,
    showMore,
    refresh,
    rename,
    remove,
  };
}

/** The pages with every conversation replaced by what `change` makes of it, or left
 * out where it makes nothing. */
function each(
  pages: ConversationList[] | undefined,
  change: (item: ConversationSummary) => ConversationSummary | null,
): ConversationList[] | undefined {
  return pages?.map((page) => ({
    ...page,
    items: page.items.flatMap((item) => change(item) ?? []),
  }));
}

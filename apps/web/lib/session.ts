// Whose session this browser has.
//
// The session itself is a cookie that page scripts can't read (ADR 0009). What the
// page holds is the API's answer to "who am I": a guest, or an account with an email
// address. The page changes that answer only with what the API said: after signing up
// or logging in, with whom the API says the session belongs to now; after logging
// out, by asking again.
//
// A page can have several sessions in its life: a guest's, then an account's, then a
// guest's again. Nothing read from the API during one of them may be shown in the
// next, even to the same account: it may be out of date by then. So everything read
// is held under the session it was read in (`holder`), and dropped when that ends.
import { useCallback, useState } from "react";
import useSWR, { useSWRConfig } from "swr";

import { startSession } from "@/lib/api/client";
import type { Me } from "@/lib/api/client";

const SESSION = "session";

export interface Session {
  /** Whose session it is. Nothing until the API has said, or when it couldn't. */
  me: Me | undefined;
  /** What to hold under, whatever is read from the API for this session: whose it
   * is, and which of this page's sessions it is. Nothing while there is none. */
  holder: string | null;
  /** Why no session could be started, if none could. */
  error: unknown;
  /** Whether the API is being asked right now. */
  starting: boolean;
  /** Ask again, after a failure. */
  retry: () => void;
  /** The session has changed hands: the API answered a sign-up or a login with whose
   * it is now. */
  become: (me: Me) => void;
  /** The session is over: forget whose it was, and start one as a new guest. */
  startOver: () => void;
}

/** This browser's session, starting one as a guest on a first visit. For the one
 * component that holds the page together: it counts the page's sessions. */
export function useSession(): Session {
  const { cache } = useSWRConfig();
  const { data, error, isValidating, mutate } = useSWR<Me, unknown>(
    SESSION,
    startSession,
    {
      // Asked once per page load. Each failure is shown, not retried behind the
      // person's back: a retry would count against the same limit.
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      shouldRetryOnError: false,
    },
  );
  // How many sessions this page has had before the one it has now.
  const [earlier, setEarlier] = useState(0);

  /** Let go of everything that was read for the session that is ending. */
  const forget = useCallback(() => {
    for (const key of Array.from(cache.keys())) {
      if (key !== SESSION) {
        cache.delete(key);
      }
    }
    setEarlier((count) => count + 1);
  }, [cache]);

  const retry = useCallback(() => {
    void mutate();
  }, [mutate]);
  const become = useCallback(
    (me: Me) => {
      forget();
      void mutate(me, { revalidate: false });
    },
    [forget, mutate],
  );
  const startOver = useCallback(() => {
    forget();
    // Nobody, until the API has answered: nothing of the session that ended may be
    // shown, or asked for, as if it were still there.
    void mutate(undefined, { revalidate: true });
  }, [forget, mutate]);

  // A failure counts once the asking is over. While it is asked again, it is starting.
  const failure =
    data === undefined && !isValidating && error !== undefined ? error : null;
  return {
    me: data,
    holder: data === undefined ? null : `${data.id}/${String(earlier)}`,
    error: failure,
    starting: data === undefined && failure === null,
    retry,
    become,
    startOver,
  };
}

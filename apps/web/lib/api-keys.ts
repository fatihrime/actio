// An account's API keys, as the API last listed them (ADR 0033, revision "an account's
// keys in its place on the page").
//
// The server is the truth about them, as about the list of conversations: after
// anything that may have changed them, the page reads them again, and shows what it
// read. It never inserts, takes out or sorts a key itself.
import { useCallback } from "react";
import useSWR from "swr";

import { listKeys } from "@/lib/api/client";
import type { KeyList } from "@/lib/api/client";

export interface ApiKeys {
  /** The account's keys and how many its plan allows, as the API last said. Nothing
   * until it has. */
  read: KeyList | null;
  /** What went wrong the last time they were read, if anything did. */
  error: unknown;
  /** Read them again. Once the reading is over, the keys as they are held then: the
   * ones read, or those read before when the reading failed. */
  refresh: () => Promise<KeyList | null>;
}

/** The API keys of whoever has the session. `holder` is what the session's data is held
 * under (`Session.holder`): nothing read for one session is shown in the next. Nothing
 * is read while there is no session. Only an account's own place asks: a guest has no
 * keys to read. */
export function useApiKeys(holder: string | null): ApiKeys {
  const { data, error, mutate } = useSWR<KeyList, unknown>(
    holder === null ? null : (["keys", holder] as const),
    listKeys,
    // A failure is shown, with a button. They are also read again whenever the window
    // is returned to, which is how a key used meanwhile, or one made or revoked in
    // another window, comes in.
    { shouldRetryOnError: false },
  );

  const refresh = useCallback(async () => (await mutate()) ?? null, [mutate]);

  return { read: data ?? null, error: error ?? null, refresh };
}

"use client";

import type { ReactNode } from "react";
import useSWR from "swr";

import { Notice, NoticeButton } from "@/components/notice";
import { startSession } from "@/lib/api/client";
import { explain } from "@/lib/chat/outcomes";

/** Shows its children once this browser has a session.
 *
 * Everything in the API needs one, a guest's included. A first visit starts a guest
 * session, without asking anything; a browser that has a session keeps it. The API
 * limits how many guests one network may start, and says how long to wait: that
 * answer is shown as it is, with a button to try again.
 */
export function SessionGate({ children }: { children: ReactNode }) {
  const session = useSWR("session", startSession, {
    // Asked once per page load. Each failure is shown, not retried behind the
    // person's back: a retry would count against the same limit.
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
    shouldRetryOnError: false,
  });

  if (session.data !== undefined) {
    return children;
  }
  if (session.error !== undefined && !session.isValidating) {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-10">
        <Notice
          tone="warning"
          actions={
            <NoticeButton onClick={() => void session.mutate()}>
              Try again
            </NoticeButton>
          }
        >
          <p className="font-medium">Actio couldn’t start a session for you.</p>
          <p>{explain(session.error)}</p>
        </Notice>
      </div>
    );
  }
  return (
    <p className="px-4 py-10 text-center text-sm text-zinc-500" role="status">
      Starting…
    </p>
  );
}

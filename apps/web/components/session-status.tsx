import { Notice, NoticeButton } from "@/components/notice";
import { explain } from "@/lib/chat/outcomes";
import type { Session } from "@/lib/session";

/** What stands where the chat will be while this browser has no session yet.
 *
 * Everything in the API needs one, a guest's included. A first visit starts a guest
 * session, without asking anything; a browser that has a session keeps it. The API
 * limits how many guests one network may start, and says how long to wait: that
 * answer is shown as it is, with a button to try again.
 */
export function SessionStatus({ session }: { session: Session }) {
  if (session.error !== null) {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-10">
        <Notice
          tone="warning"
          actions={
            <NoticeButton onClick={session.retry}>Try again</NoticeButton>
          }
        >
          <p className="font-medium">Actio couldn’t start a session for you.</p>
          <p>{explain(session.error)}</p>
        </Notice>
      </div>
    );
  }
  return (
    <p
      className="px-4 py-10 text-center text-sm text-muted-foreground"
      role="status"
    >
      Starting…
    </p>
  );
}

"use client";

import { InAppLink } from "@/components/in-app-link";
import { SessionStatus } from "@/components/session-status";
import type { Me } from "@/lib/api/client";
import { ACCOUNT, LOGIN, SIGNUP } from "@/lib/location";
import type { Session } from "@/lib/session";

import { Account } from "./account";
import { Panel, TEXT_LINK } from "./form";
import { LogInForm } from "./log-in";
import { SignUpForm } from "./sign-up";

/** One of the three places for the session: logging in, signing up, the account.
 *
 * What stands there depends on whose session this browser has:
 *
 * - a guest, or nobody, can log in and sign up. Nobody: when no guest session could be
 *   started, someone who has an account must still be able to reach it;
 * - an account can see to itself, and is told so where a guest would log in.
 */
export function SessionPlace({
  place,
  session,
  hasConversations,
  onEntered,
  onLoggedOut,
}: {
  place: "login" | "signup" | "account";
  session: Session;
  /** Whether whoever has the session has started any conversation: a guest who has
   * is told what becomes of them. */
  hasConversations: boolean;
  /** Called after signing up or logging in, with whose session it is now. */
  onEntered: (me: Me) => void;
  /** Called when this browser's session has ended. */
  onLoggedOut: () => void;
}) {
  const { me } = session;
  // Not before the API has said whose session this is. A form sent while a guest
  // session is still being started could be overtaken by it: the browser would end
  // up with the guest's cookie, after a login that had worked.
  if (session.starting) {
    return <SessionStatus session={session} />;
  }
  const guest = me?.is_guest === true;

  if (place === "account") {
    if (me === undefined) {
      return <SessionStatus session={session} />;
    }
    return guest ? (
      <Panel title="Your account">
        <p>
          You are using Actio as a guest. A guest’s conversations are kept for a
          day.
        </p>
        <p>
          <InAppLink href={SIGNUP} className={TEXT_LINK}>
            Sign up
          </InAppLink>{" "}
          to keep them, or{" "}
          <InAppLink href={LOGIN} className={TEXT_LINK}>
            log in
          </InAppLink>{" "}
          if you have an account.
        </p>
      </Panel>
    ) : (
      <Account me={me} onLoggedOut={onLoggedOut} />
    );
  }

  if (me !== undefined && !guest) {
    return (
      <Panel title={place === "login" ? "Log in" : "Create an account"}>
        <p>
          You are logged in as <strong className="break-all">{me.email}</strong>
          .
        </p>
        <p>
          <InAppLink href="/" className={TEXT_LINK}>
            Go to the chat
          </InAppLink>
          , or see to{" "}
          <InAppLink href={ACCOUNT} className={TEXT_LINK}>
            your account
          </InAppLink>
          , where you can log out.
        </p>
      </Panel>
    );
  }
  const keeps = guest && hasConversations;
  return place === "login" ? (
    <LogInForm keeps={keeps} onDone={onEntered} />
  ) : (
    <SignUpForm keeps={keeps} onDone={onEntered} />
  );
}

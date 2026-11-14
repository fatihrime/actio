"use client";

import { useState } from "react";
import type { SyntheticEvent } from "react";

import { Notice, NoticeButton } from "@/components/notice";
import { newPasswordProblem, PASSWORD_MAX, PASSWORD_MIN } from "@/lib/account";
import {
  ApiProblem,
  changePassword,
  logOut,
  logOutEverywhere,
} from "@/lib/api/client";
import type { Me } from "@/lib/api/client";
import { explain, isSessionEnd } from "@/lib/chat/outcomes";

import { Field, focusOn, Panel, SubmitButton, typed } from "./form";

const PLAIN_BUTTON =
  "rounded-lg border border-input px-4 py-2 text-sm font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-60";

/** An account, seen by its owner: whose it is, a new password for it, and the ways
 * out of it. */
export function Account({
  me,
  onLoggedOut,
}: {
  me: Me;
  /** Called when this browser's session has ended. */
  onLoggedOut: () => void;
}) {
  return (
    <Panel title="Your account">
      <p>
        You are logged in as <strong className="break-all">{me.email}</strong>.
      </p>
      <ChangePassword email={me.email ?? ""} onSessionEnded={onLoggedOut} />
      <LogOut onLoggedOut={onLoggedOut} />
    </Panel>
  );
}

/** The form that replaces the password. It asks for the current one: a browser left
 * logged in is not enough to take an account over. */
function ChangePassword({
  email,
  onSessionEnded,
}: {
  email: string;
  onSessionEnded: () => void;
}) {
  const [wrong, setWrong] = useState<{
    current?: string | null;
    next?: string | null;
  }>({});
  const [problem, setProblem] = useState<unknown>(null);
  const [state, setState] = useState<"idle" | "busy" | "changed">("idle");

  async function submit(form: HTMLFormElement) {
    const data = new FormData(form);
    const current = typed(data, "current");
    const next = typed(data, "next");
    const found = {
      current: current === "" ? "Enter your current password." : null,
      next: newPasswordProblem(next),
    };
    setWrong(found);
    setProblem(null);
    if (found.current !== null || found.next !== null) {
      setState("idle");
      focusOn(form, found.current !== null ? "current" : "next");
      return;
    }
    setState("busy");
    try {
      await changePassword(current, next);
    } catch (error) {
      setState("idle");
      if (error instanceof ApiProblem && error.status === 403) {
        setWrong({ current: error.message });
        focusOn(form, "current");
      } else if (
        error instanceof ApiProblem &&
        error.invalid.includes("new_password")
      ) {
        setWrong({
          next: `Use ${String(PASSWORD_MIN)} to ${String(PASSWORD_MAX)} characters.`,
        });
        focusOn(form, "next");
      } else {
        setProblem(error);
      }
      return;
    }
    form.reset(); // neither password stays on the page
    setState("changed");
  }

  return (
    <form
      noValidate
      aria-labelledby="change-password-heading"
      className="space-y-4 border-t border-border pt-6"
      onSubmit={(event: SyntheticEvent<HTMLFormElement>) => {
        event.preventDefault();
        if (state !== "busy") {
          void submit(event.currentTarget);
        }
      }}
    >
      <h3 id="change-password-heading" className="text-lg font-semibold">
        Change your password
      </h3>
      {state === "changed" && (
        <Notice>
          Your password is changed. Every other browser was logged out.
        </Notice>
      )}
      {problem !== null && (
        <Notice
          tone="warning"
          actions={
            isSessionEnd(problem) && (
              <NoticeButton onClick={onSessionEnded}>
                Start a new session
              </NoticeButton>
            )
          }
        >
          {explain(problem)}
        </Notice>
      )}
      {/* For a password manager: whose passwords these are. Nobody sees or types it. */}
      <input
        type="email"
        name="email"
        autoComplete="username"
        value={email}
        readOnly
        hidden
      />
      <Field
        label="Current password"
        name="current"
        type="password"
        autoComplete="current-password"
        maxLength={PASSWORD_MAX}
        error={wrong.current}
      />
      <Field
        label="New password"
        name="next"
        type="password"
        autoComplete="new-password"
        maxLength={PASSWORD_MAX}
        error={wrong.next}
        hint={`At least ${String(PASSWORD_MIN)} characters. Every other browser is logged out by the change.`}
      />
      <SubmitButton busy={state === "busy"} whileBusy="Changing…">
        Change password
      </SubmitButton>
    </form>
  );
}

/** The two ways out: of this browser, or of every browser at once. */
function LogOut({ onLoggedOut }: { onLoggedOut: () => void }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);

  async function leave(everywhere: boolean) {
    setBusy(true);
    setProblem(null);
    try {
      await (everywhere ? logOutEverywhere() : logOut());
    } catch (error) {
      // A session that had ended already is as logged out as it gets.
      if (!isSessionEnd(error)) {
        setBusy(false);
        setProblem(error);
        return;
      }
    }
    onLoggedOut();
  }

  return (
    <section
      aria-labelledby="log-out-heading"
      className="space-y-4 border-t border-border pt-6"
    >
      <h3 id="log-out-heading" className="text-lg font-semibold">
        Log out
      </h3>
      {problem !== null && <Notice tone="warning">{explain(problem)}</Notice>}
      <p className="text-sm text-muted-foreground">
        Your conversations stay with the account. Logging out everywhere ends
        its sessions in every browser, this one included.
      </p>
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          disabled={busy}
          onClick={() => void leave(false)}
          className={PLAIN_BUTTON}
        >
          Log out
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void leave(true)}
          className={PLAIN_BUTTON}
        >
          Log out everywhere
        </button>
      </div>
    </section>
  );
}

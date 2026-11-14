"use client";

import { useState } from "react";
import type { SyntheticEvent } from "react";

import { InAppLink } from "@/components/in-app-link";
import { Notice } from "@/components/notice";
import { EMAIL_MAX, PASSWORD_MAX } from "@/lib/account";
import { ApiProblem, logIn } from "@/lib/api/client";
import type { Me } from "@/lib/api/client";
import { explain } from "@/lib/chat/outcomes";
import { SIGNUP } from "@/lib/location";

import { Field, focusOn, Panel, SubmitButton, TEXT_LINK, typed } from "./form";

/** The form that logs in to an account.
 *
 * It asks nothing of the form of an address or a password: whatever is wrong with
 * them, the API's answer is the same, "wrong email address or password", so the form
 * can't be used to find out who has an account (ADR 0010).
 */
export function LogInForm({
  keeps,
  onDone,
}: {
  /** Whether a guest is logging in who has started conversations, which go along. */
  keeps: boolean;
  /** Called with whose session it is now. */
  onDone: (me: Me) => void;
}) {
  const [wrong, setWrong] = useState<{
    email?: string | null;
    password?: string | null;
  }>({});
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(form: HTMLFormElement) {
    const data = new FormData(form);
    const email = typed(data, "email").trim();
    const password = typed(data, "password");
    const found = {
      email: email === "" ? "Enter your email address." : null,
      password: password === "" ? "Enter your password." : null,
    };
    setWrong(found);
    setProblem(null);
    if (found.email !== null || found.password !== null) {
      focusOn(form, found.email !== null ? "email" : "password");
      return;
    }
    setBusy(true);
    try {
      onDone(await logIn(email, password));
    } catch (error) {
      setBusy(false);
      // The API's own words: that the two don't match, or how long to wait after too
      // many attempts. Here a 401 is not a session that ended.
      setProblem(error instanceof ApiProblem ? error.message : explain(error));
      focusOn(form, "password");
    }
  }

  return (
    <Panel title="Log in">
      {keeps && (
        <p className="text-sm text-muted-foreground">
          The conversations you started as a guest come with you.
        </p>
      )}
      <form
        noValidate
        className="space-y-4"
        onSubmit={(event: SyntheticEvent<HTMLFormElement>) => {
          event.preventDefault();
          if (!busy) {
            void submit(event.currentTarget);
          }
        }}
      >
        {problem !== null && <Notice tone="warning">{problem}</Notice>}
        <Field
          label="Email address"
          name="email"
          type="email"
          autoComplete="username"
          maxLength={EMAIL_MAX}
          error={wrong.email}
        />
        <Field
          label="Password"
          name="password"
          type="password"
          autoComplete="current-password"
          maxLength={PASSWORD_MAX}
          error={wrong.password}
        />
        <SubmitButton busy={busy} whileBusy="Logging in…">
          Log in
        </SubmitButton>
      </form>
      <p className="text-sm text-muted-foreground">
        No account yet?{" "}
        <InAppLink href={SIGNUP} replace className={TEXT_LINK}>
          Sign up
        </InAppLink>
      </p>
    </Panel>
  );
}

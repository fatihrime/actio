"use client";

import { useState } from "react";
import type { SyntheticEvent } from "react";

import { InAppLink } from "@/components/in-app-link";
import { Notice } from "@/components/notice";
import {
  EMAIL_MAX,
  emailProblem,
  newPasswordProblem,
  PASSWORD_MAX,
  PASSWORD_MIN,
} from "@/lib/account";
import { ApiProblem, signUp } from "@/lib/api/client";
import type { Me } from "@/lib/api/client";
import { explain } from "@/lib/chat/outcomes";
import { LOGIN } from "@/lib/location";

import { Field, focusOn, Panel, SubmitButton, TEXT_LINK, typed } from "./form";

/** The form that makes a lasting account.
 *
 * Someone who is a guest becomes the account: the same one, with an email address and
 * a password now, so nothing they did as a guest is lost (ADR 0010).
 */
export function SignUpForm({
  keeps,
  onDone,
}: {
  /** Whether a guest is signing up who has started conversations, to keep. */
  keeps: boolean;
  /** Called with whose session it is now. */
  onDone: (me: Me) => void;
}) {
  // What is wrong with each box, and what went wrong with the whole attempt.
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
      email: emailProblem(email),
      password: newPasswordProblem(password),
    };
    setWrong(found);
    setProblem(null);
    if (found.email !== null || found.password !== null) {
      focusOn(form, found.email !== null ? "email" : "password");
      return;
    }
    setBusy(true);
    try {
      onDone(await signUp(email, password));
    } catch (error) {
      setBusy(false);
      if (error instanceof ApiProblem && error.status === 409) {
        // The address has an account. Without email there is no way to keep that
        // to oneself and still say why nothing happened (ADR 0010).
        setWrong({ email: error.message });
        focusOn(form, "email");
      } else if (
        error instanceof ApiProblem &&
        (error.invalid.includes("email") || error.invalid.includes("password"))
      ) {
        // The API is stricter about an address than the check above.
        setWrong({
          email: error.invalid.includes("email")
            ? "The server doesn’t take this as an email address."
            : null,
          password: error.invalid.includes("password")
            ? `Use ${String(PASSWORD_MIN)} to ${String(PASSWORD_MAX)} characters.`
            : null,
        });
        focusOn(form, error.invalid.includes("email") ? "email" : "password");
      } else {
        // The API's own words, such as how long to wait after too many attempts.
        setProblem(
          error instanceof ApiProblem ? error.message : explain(error),
        );
      }
    }
  }

  return (
    <Panel title="Create an account">
      {keeps && (
        <p className="text-sm text-muted-foreground">
          What you did as a guest stays yours: the account takes over your
          conversations.
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
          autoComplete="new-password"
          maxLength={PASSWORD_MAX}
          error={wrong.password}
          hint={`At least ${String(PASSWORD_MIN)} characters. Choose one you will remember: Actio sends no email, so it can’t send you a new one.`}
        />
        <SubmitButton busy={busy} whileBusy="Signing up…">
          Sign up
        </SubmitButton>
      </form>
      <p className="text-sm text-muted-foreground">
        Already have an account?{" "}
        <InAppLink href={LOGIN} replace className={TEXT_LINK}>
          Log in
        </InAppLink>
      </p>
    </Panel>
  );
}

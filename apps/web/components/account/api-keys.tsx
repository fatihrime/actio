"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { Ref, SyntheticEvent } from "react";

import { Notice, NoticeButton } from "@/components/notice";
import { KEY_NAME_MAX, KEY_NAME_MIN, keyNameProblem } from "@/lib/account";
import { ApiProblem, makeKey, revokeKey } from "@/lib/api/client";
import type { ApiKey, KeyList } from "@/lib/api/client";
import { useApiKeys } from "@/lib/api-keys";
import { explain, isSessionEnd } from "@/lib/chat/outcomes";
import { onDay, useNow } from "@/lib/time";

import { Field, focusOn, PLAIN_BUTTON, SubmitButton, typed } from "./form";

/** How often the list looks at the clock: whether a key was made or used today is the
 * only thing in it that depends on it. */
const MINUTE_MS = 60_000;

/** A key that was just made: what it is called, and the whole of it, which no other
 * answer of the API will hold. */
interface Made {
  name: string;
  key: string;
}

/** What stands where the form is: nothing until the keys are read; then the key that
 * was just made, for as long as it is shown; the form, while the plan allows another
 * key; or what to do to make one. With a plan that allows none, nothing. */
type FormsPlace = "unread" | "made" | "form" | "as-many" | "more" | "nothing";

function formsPlace(read: KeyList | null, made: Made | null): FormsPlace {
  if (made !== null) {
    return "made";
  }
  if (read === null) {
    return "unread";
  }
  const { keys, at_most: atMost } = read;
  if (atMost === 0) {
    return "nothing";
  }
  if (keys.length < atMost) {
    return "form";
  }
  return keys.length === atMost ? "as-many" : "more";
}

/** What the plan allows, from what the API said. */
function allowed({ keys, at_most: atMost }: KeyList): string {
  if (atMost > 0) {
    return `Your plan allows ${String(atMost)} at once.`;
  }
  return keys.length === 0
    ? "Your plan allows none."
    : "Your plan allows none now: the keys below are refused until it does.";
}

/** What came of a try at making a key: whether it was made, and when it wasn't, what
 * is wrong with its name, if the API refused it for that. Anything else that went
 * wrong is said in the section's notice. */
interface Tried {
  made: boolean;
  wrong: string | null;
}

/** The id of a key's "Revoke", by which the keyboard is put on it. */
function revokeButtonId(keyId: string): string {
  return `revoke-key-${keyId}`;
}

/** Whether the keyboard is on nothing, as when the element that had it left the page:
 * that says nothing of it, and leaves the keyboard to the page's body. */
function keyboardLost(): boolean {
  const on = document.activeElement;
  return on === null || on === document.body;
}

/** The account's API keys: what its plan allows, the form that makes one, a key just
 * made, shown whole once, and the list, where each can be revoked (ADR 0033, revision
 * "an account's keys in its place on the page").
 *
 * What is listed is what the API said last (`lib/api-keys.ts`). After anything that may
 * have changed the keys, they are read again, and nothing is inserted, taken out or
 * sorted here. A key that was just made is held in this component's state alone, while
 * it is shown: "Done", leaving the place and the end of the session take it away. */
export function ApiKeys({
  holder,
  onSessionEnded,
}: {
  /** What the session's data is held under (`Session.holder`). */
  holder: string | null;
  /** Called when this browser's session turns out to have ended. */
  onSessionEnded: () => void;
}) {
  const keys = useApiKeys(holder);
  const { read } = keys;
  const [made, setMade] = useState<Made | null>(null);
  // What went wrong with the last making or revoking. A failure to read the keys is
  // the hook's own (`keys.error`).
  const [problem, setProblem] = useState<unknown>(null);
  const failure = problem ?? keys.error;
  const place = formsPlace(read, made);

  const heading = useRef<HTMLHeadingElement>(null);
  const form = useRef<HTMLFormElement>(null);

  // "Try again" goes with its notice once the keys are read: the keyboard, which was
  // on it, goes on the heading.
  const triedAgain = useRef(false);
  useLayoutEffect(() => {
    if (failure === null && triedAgain.current) {
      triedAgain.current = false;
      if (keyboardLost()) {
        heading.current?.focus();
      }
    }
  }, [failure]);

  // After "Done", the keyboard goes on the name's box when the form is there then, and
  // otherwise on the heading.
  const doneJustNow = useRef(false);
  useLayoutEffect(() => {
    if (made !== null || !doneJustNow.current) {
      return;
    }
    doneJustNow.current = false;
    if (form.current !== null) {
      focusOn(form.current, "name");
    } else {
      heading.current?.focus();
    }
  }, [made]);

  // Whether the keyboard was last put on something in the form. When the form gives
  // way to a sentence, or to nothing, while it is there, as after a refusal whose
  // reading of the keys says that the plan's number is reached, the keyboard goes on
  // the heading. A key that was made puts it on the key's box instead (`MadeKey`).
  const inForm = useRef(false);
  useLayoutEffect(() => {
    if (
      (place === "as-many" || place === "more" || place === "nothing") &&
      inForm.current
    ) {
      inForm.current = false;
      if (keyboardLost()) {
        heading.current?.focus();
      }
    }
  }, [place]);

  /** Make a key called `name`. What is wrong with the name, when the API refused it
   * for that, is said under the box. */
  async function make(name: string): Promise<Tried> {
    setProblem(null);
    try {
      const answer = await makeKey(name);
      setMade({ name: answer.name, key: answer.key });
      return { made: true, wrong: null };
    } catch (error) {
      if (error instanceof ApiProblem && error.invalid.includes("name")) {
        return {
          made: false,
          wrong: `Use ${String(KEY_NAME_MIN)} to ${String(KEY_NAME_MAX)} characters.`,
        };
      }
      if (
        error instanceof ApiProblem &&
        (error.status === 403 || error.status === 409)
      ) {
        // The plan allows none, or no more: in the API's words, which name it.
        return { made: false, wrong: error.message };
      }
      setProblem(error);
      return { made: false, wrong: null };
    } finally {
      // After every try, made or not. A refusal says that the list was out of date,
      // and an answer that never arrived may have made a key all the same, which the
      // list then shows, to be revoked.
      void keys.refresh();
    }
  }

  /** Revoke a key. Whether it is gone, by the keys read afterwards: until then its row
   * stays. */
  async function revoke(apiKey: ApiKey): Promise<boolean> {
    setProblem(null);
    // Where the keyboard goes once it is gone: the key listed after it, or else the one
    // before it, while the list still has that one; otherwise the heading.
    const listed = read?.keys ?? [];
    const at = listed.findIndex((each) => each.id === apiKey.id);
    const neighbours =
      at === -1
        ? []
        : [listed[at + 1], listed[at - 1]].flatMap((each) =>
            each === undefined ? [] : [each.id],
          );
    let failed: unknown = null;
    try {
      await revokeKey(apiKey.id);
    } catch (error) {
      // A 404 is a key revoked already, in another window: gone is what was asked for.
      if (!(error instanceof ApiProblem && error.status === 404)) {
        failed = error;
      }
    }
    // Read again whatever was answered: an answer that never arrived may have revoked
    // the key all the same.
    const after = await keys.refresh();
    const still = new Set(after?.keys.map((each) => each.id));
    if (still.has(apiKey.id)) {
      // Not revoked, or the keys couldn't be read again: the notice says which.
      setProblem(failed);
      return false;
    }
    // The key after it, unless its own question stands in its row's place just now.
    const next = neighbours.find((id) => still.has(id));
    (
      (next === undefined
        ? null
        : document.getElementById(revokeButtonId(next))) ?? heading.current
    )?.focus();
    return true;
  }

  return (
    <section
      aria-labelledby="api-keys-heading"
      className="space-y-4 border-t border-border pt-6"
      onFocus={(event) => {
        const on = event.target;
        inForm.current =
          on instanceof Node && form.current?.contains(on) === true;
      }}
    >
      <h3
        ref={heading}
        id="api-keys-heading"
        tabIndex={-1}
        className="text-lg font-semibold outline-none"
      >
        API keys
      </h3>
      <p className="text-sm text-muted-foreground">
        An API key lets a program use Actio as this account.
        {read !== null && ` ${allowed(read)}`}
      </p>
      {failure !== null && (
        <Notice
          tone="warning"
          actions={
            isSessionEnd(problem) || isSessionEnd(keys.error) ? (
              <NoticeButton onClick={onSessionEnded}>
                Start a new session
              </NoticeButton>
            ) : keys.error !== null ? (
              <NoticeButton
                onClick={() => {
                  triedAgain.current = true;
                  setProblem(null);
                  void keys.refresh();
                }}
              >
                Try again
              </NoticeButton>
            ) : undefined
          }
        >
          {explain(failure)}
        </Notice>
      )}
      {place === "unread" && keys.error === null && (
        <p role="status" className="text-sm text-muted-foreground">
          Reading your keys…
        </p>
      )}
      {made !== null && (
        <MadeKey
          made={made}
          onDone={() => {
            doneJustNow.current = true;
            setMade(null);
          }}
        />
      )}
      {place === "form" && <MakeForm ref={form} onMake={make} />}
      {place === "as-many" && (
        <p className="text-sm">
          You have as many keys as your plan allows. Revoke one to make another.
        </p>
      )}
      {place === "more" && (
        <p className="text-sm">
          You have more keys than your plan allows. Revoke some to make another.
        </p>
      )}
      {read !== null && read.keys.length > 0 && (
        <KeyRows keys={read.keys} onRevoke={revoke} />
      )}
    </section>
  );
}

/** The form that makes a key: its name, and the button. */
function MakeForm({
  ref,
  onMake,
}: {
  ref: Ref<HTMLFormElement>;
  /** Make a key of this name, and say what came of it. */
  onMake: (name: string) => Promise<Tried>;
}) {
  const [wrong, setWrong] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(form: HTMLFormElement) {
    const name = typed(new FormData(form), "name").trim();
    const found = keyNameProblem(name);
    setWrong(found);
    if (found !== null) {
      focusOn(form, "name");
      return;
    }
    setBusy(true);
    const tried = await onMake(name);
    // Made, the form is gone, and the key is in its place, with the keyboard.
    setBusy(false);
    setWrong(tried.wrong);
    if (!tried.made) {
      // Refused for its name, or failed for another reason, which the notice says: the
      // name is still in the box, to be changed or sent again.
      focusOn(form, "name");
    }
  }

  return (
    <form
      ref={ref}
      noValidate
      className="space-y-4"
      onSubmit={(event: SyntheticEvent<HTMLFormElement>) => {
        event.preventDefault();
        if (!busy) {
          void submit(event.currentTarget);
        }
      }}
    >
      <Field
        label="Name"
        name="name"
        // No password's and no address's: nothing for a browser to fill in.
        autoComplete="off"
        maxLength={KEY_NAME_MAX}
        error={wrong}
        hint="What to call the key, so that you can tell it from your others."
      />
      <SubmitButton busy={busy} whileBusy="Making…">
        Make a key
      </SubmitButton>
    </form>
  );
}

/** A key that was just made, whole, in one box: the only time it is ever shown. */
function MadeKey({ made, onDone }: { made: Made; onDone: () => void }) {
  const id = useId();
  const box = useRef<HTMLTextAreaElement>(null);
  const copyButton = useRef<HTMLButtonElement>(null);
  // What became of the last "Copy". On the page, and empty, from the first: a screen
  // reader is told what comes into it.
  const [said, setSaid] = useState("");

  // All of it selected, with the keyboard on it: so a copy by the keyboard works at
  // once, in every browser, and at an address where the page has no clipboard.
  useEffect(() => {
    selectAll(box.current);
  }, []);

  async function copy() {
    if (await copied(made.key)) {
      setSaid("Copied.");
      // Put there, not left to the click: WebKit puts the keyboard on no button that
      // is clicked.
      copyButton.current?.focus();
      return;
    }
    selectAll(box.current);
    setSaid(
      "It couldn’t be copied for you. It is selected: copy it with your keyboard.",
    );
  }

  return (
    <div className="space-y-2">
      <label
        htmlFor={`${id}-key`}
        className="block text-sm font-medium wrap-anywhere"
      >
        Your new key, “{made.name}”
      </label>
      {/* Two rows of a font whose letters are all as wide, broken anywhere: the 56
          characters stand in it whole, in a window 320 px wide. With letters larger
          than the browser's own they take a third row there, and the box grows to
          hold it, as high as what is in it. */}
      <textarea
        ref={box}
        id={`${id}-key`}
        value={made.key}
        readOnly
        rows={2}
        aria-describedby={`${id}-about`}
        // Nothing for the browser to check, fill in or change.
        spellCheck={false}
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        className="field-sizing-content w-full resize-none rounded-lg border border-input bg-card px-3 py-2 font-mono text-sm break-all outline-none focus:border-ring focus:ring-2 focus:ring-ring/30"
      />
      <p id={`${id}-about`} className="text-xs text-muted-foreground">
        Copy it now, and keep it as you keep a password: it isn’t shown again.
      </p>
      <div className="flex flex-wrap gap-3">
        <button
          ref={copyButton}
          type="button"
          onClick={() => void copy()}
          className={PLAIN_BUTTON}
        >
          Copy
        </button>
        <button type="button" onClick={onDone} className={PLAIN_BUTTON}>
          Done
        </button>
      </div>
      <p role="status" className="text-sm">
        {said}
      </p>
    </div>
  );
}

function selectAll(box: HTMLTextAreaElement | null): void {
  box?.focus();
  box?.select();
}

/** Whether `text` was put on the clipboard. A page at a plain http address other than
 * localhost has no clipboard to write to, and a browser may refuse to. */
async function copied(text: string): Promise<boolean> {
  const clipboard = navigator.clipboard as Clipboard | undefined;
  if (clipboard === undefined) {
    return false;
  }
  try {
    await clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** The account's keys, in the API's order. */
function KeyRows({
  keys,
  onRevoke,
}: {
  keys: ApiKey[];
  /** Whether the key is gone. */
  onRevoke: (apiKey: ApiKey) => Promise<boolean>;
}) {
  const now = useNow(MINUTE_MS);
  return (
    <ul aria-label="Your API keys">
      {keys.map((apiKey) => (
        <KeyRow
          key={apiKey.id}
          apiKey={apiKey}
          now={now}
          onRevoke={() => onRevoke(apiKey)}
        />
      ))}
    </ul>
  );
}

/** One key: its name, its prefix, when it was made and last used, and the button that
 * revokes it, which asks first, in the row's place: there is no way back. */
function KeyRow({
  apiKey,
  now,
  onRevoke,
}: {
  apiKey: ApiKey;
  now: number;
  /** Whether the key is gone. */
  onRevoke: () => Promise<boolean>;
}) {
  const [mode, setMode] = useState<"idle" | "asking" | "revoking">("idle");

  // After deciding not to revoke, or a revoking that failed, the keyboard goes back to
  // the button it came from.
  const button = useRef<HTMLButtonElement>(null);
  const goBack = useRef(false);
  useEffect(() => {
    if (mode === "idle" && goBack.current) {
      button.current?.focus();
      goBack.current = false;
    }
  }, [mode]);

  async function revoke() {
    setMode("revoking");
    if (!(await onRevoke())) {
      goBack.current = true;
      setMode("idle");
    }
    // Revoked, the key is no longer listed, and this row is no longer on the page.
  }

  if (mode !== "idle") {
    return (
      <li className="border-t border-border py-3">
        <ConfirmRevoke
          name={apiKey.name}
          revoking={mode === "revoking"}
          onRevoke={() => void revoke()}
          onCancel={() => {
            goBack.current = true;
            setMode("idle");
          }}
        />
      </li>
    );
  }
  const at = new Date(now);
  return (
    <li className="flex items-start gap-3 border-t border-border py-3">
      <div className="min-w-0 flex-1 space-y-0.5 text-sm">
        <p className="font-medium wrap-anywhere">{apiKey.name}</p>
        <p className="font-mono text-muted-foreground">{apiKey.prefix}…</p>
        <p className="text-xs text-muted-foreground">
          Made {onDay(new Date(apiKey.created_at), at)}
        </p>
        <p className="text-xs text-muted-foreground">
          {apiKey.last_used_at === null
            ? "Never used"
            : `Last used ${onDay(new Date(apiKey.last_used_at), at)}`}
        </p>
      </div>
      <button
        ref={button}
        id={revokeButtonId(apiKey.id)}
        type="button"
        onClick={() => {
          setMode("asking");
        }}
        aria-label={`Revoke “${apiKey.name}”`}
        className="shrink-0 rounded-md border border-input px-3 py-1 text-sm font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      >
        Revoke
      </button>
    </li>
  );
}

/** Asks before a key is revoked. */
function ConfirmRevoke({
  name,
  revoking,
  onRevoke,
  onCancel,
}: {
  name: string;
  revoking: boolean;
  onRevoke: () => void;
  onCancel: () => void;
}) {
  // The keyboard lands on the harmless answer.
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancel.current?.focus();
  }, []);

  return (
    <div
      role="group"
      aria-label={`Revoke “${name}”?`}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !revoking) {
          // Escape has a use of its own around it, for the list over a narrow window.
          event.stopPropagation();
          onCancel();
        }
      }}
      className="space-y-2 rounded-lg border border-border bg-card p-3 text-sm"
    >
      <p className="font-medium wrap-anywhere">{name}</p>
      <p className="text-muted-foreground">
        Revoke this key? Whatever uses it is refused from then on, and it can’t
        be brought back.
      </p>
      <div className="flex justify-end gap-2">
        <button
          ref={cancel}
          type="button"
          onClick={onCancel}
          disabled={revoking}
          className="rounded-md border border-input px-3 py-1 font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-40"
        >
          Cancel
        </button>
        {/* A filled ground, which a system's own colours take away: there it has a
            line around it (app/globals.css). */}
        <button
          type="button"
          onClick={onRevoke}
          disabled={revoking}
          className="rounded-md bg-destructive px-3 py-1 font-medium text-destructive-foreground hover:bg-destructive/85 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60 forced-colors:border"
        >
          {revoking ? "Revoking…" : "Revoke"}
        </button>
      </div>
    </div>
  );
}

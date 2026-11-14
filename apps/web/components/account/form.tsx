"use client";

// What the three forms for a session are made of: a column to stand in, a labelled
// box, a button that says when it is busy.
import { useId, useState } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";

/** The column a form stands in, under its heading. It scrolls by itself: the page
 * around it never does. */
export function Panel({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <section
        aria-labelledby="place-heading"
        className="mx-auto w-full max-w-sm space-y-6 px-4 py-10"
      >
        <h2
          id="place-heading"
          className="text-2xl font-semibold tracking-tight"
        >
          {title}
        </h2>
        {children}
      </section>
    </div>
  );
}

// The ring says where the keyboard is, also on a box whose edge is red already.
const BOX =
  "w-full rounded-lg border border-input bg-card px-3 py-2 outline-none focus:border-ring focus:ring-2 focus:ring-ring/30 aria-[invalid=true]:border-destructive";

/** A labelled box of a form. What is wrong with what was typed stands underneath, and
 * is read out with the box. A password can be shown while it is typed: there is no
 * second box to type it again. */
export function Field({
  label,
  hint,
  error,
  type = "text",
  ...box
}: {
  label: string;
  /** What to know before typing. */
  hint?: ReactNode;
  /** What is wrong with what was typed, if anything. */
  error?: string | null;
} & Omit<
  InputHTMLAttributes<HTMLInputElement>,
  "id" | "className" | "aria-invalid" | "aria-describedby"
>) {
  const id = useId();
  const [shown, setShown] = useState(false);
  const wrong = error !== undefined && error !== null;
  const about = [
    hint !== undefined ? `${id}-hint` : null,
    wrong ? `${id}-error` : null,
  ].filter((part) => part !== null);
  const input = (
    <input
      id={id}
      type={type === "password" && shown ? "text" : type}
      aria-invalid={wrong ? true : undefined}
      aria-describedby={about.length > 0 ? about.join(" ") : undefined}
      className={type === "password" ? `${BOX} pr-16` : BOX}
      {...box}
    />
  );

  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      {type === "password" ? (
        <div className="relative">
          {input}
          <button
            type="button"
            onClick={() => {
              setShown(!shown);
            }}
            aria-pressed={shown}
            aria-label={`Show the ${label.toLowerCase()}`}
            className="absolute inset-y-0 right-0 rounded-r-lg px-3 text-sm text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
          >
            {shown ? "Hide" : "Show"}
          </button>
        </div>
      ) : (
        input
      )}
      {hint !== undefined && (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
      {wrong && (
        <p id={`${id}-error`} className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

/** The button that sends a form. While the answer is awaited it says so, and can't be
 * pressed again. */
export function SubmitButton({
  busy,
  children,
  whileBusy,
}: {
  busy: boolean;
  children: ReactNode;
  whileBusy: string;
}) {
  return (
    <button
      type="submit"
      disabled={busy}
      className="w-full rounded-lg bg-primary px-4 py-2 font-medium text-primary-foreground hover:bg-primary/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-60"
    >
      {busy ? whileBusy : children}
    </button>
  );
}

/** What a form holds under the name of one of its boxes, as text. */
export function typed(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

/** Put the keyboard on a form's box, by its name: the first one that needs fixing. */
export function focusOn(form: HTMLFormElement, name: string): void {
  const box = form.elements.namedItem(name);
  if (box instanceof HTMLElement) {
    box.focus();
  }
}

export const TEXT_LINK = "font-medium underline underline-offset-2";

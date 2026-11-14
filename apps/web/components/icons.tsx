// The few pictures on buttons. Drawn here, as lines on a 16 by 16 grid in the colour
// of the text around them. A picture says nothing to a screen reader: the button it
// is on carries a label.
import type { ReactNode } from "react";

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden="true"
      className="size-4 shrink-0"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  );
}

/** A window with a panel at its side: the list of conversations. */
export function PanelIcon() {
  return (
    <Icon>
      <rect x="1.75" y="2.75" width="12.5" height="10.5" rx="2" />
      <path d="M6 2.75v10.5" />
    </Icon>
  );
}

/** A plus: something new. */
export function PlusIcon() {
  return (
    <Icon>
      <path d="M8 3v10M3 8h10" />
    </Icon>
  );
}

/** A pencil: rename. */
export function PencilIcon() {
  return (
    <Icon>
      <path d="M2.75 13.25l.75-3L10.5 3.25l2.25 2.25-7 7z" />
      <path d="M9 4.75L11.25 7" />
    </Icon>
  );
}

/** A bin: delete. */
export function BinIcon() {
  return (
    <Icon>
      <path d="M2.75 4.5h10.5" />
      <path d="M6.25 4.5V2.75h3.5V4.5" />
      <path d="M4.25 4.5l.5 8.75h6.5l.5-8.75" />
    </Icon>
  );
}

import type { ReactNode } from "react";

const TONES = {
  plain: "border-border bg-sidebar text-foreground",
  warning: "border-warning-border bg-warning text-warning-foreground",
};

/** A short message about something that needs the person's attention, with the
 * buttons to act on it. `role="status"` has a screen reader say it when it appears. */
export function Notice({
  tone = "plain",
  children,
  actions,
}: {
  tone?: keyof typeof TONES;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div
      role="status"
      className={`flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-lg border px-4 py-3 text-sm ${TONES[tone]}`}
    >
      <div className="min-w-0 space-y-1">{children}</div>
      {actions !== undefined && (
        <div className="flex shrink-0 gap-2">{actions}</div>
      )}
    </div>
  );
}

/** A button for inside a notice. */
export function NoticeButton({
  onClick,
  children,
}: {
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-md border border-current/30 px-3 py-1 font-medium hover:bg-current/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-current"
    >
      {children}
    </button>
  );
}

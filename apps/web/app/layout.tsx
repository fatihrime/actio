import type { Metadata } from "next";
import type { ReactNode } from "react";

import { LIST_AWAY_SCRIPT } from "@/lib/frame";

import "./globals.css";

export const metadata: Metadata = {
  title: "Actio",
  description: "A self-hostable, local-first AI platform.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    // The script below marks this element before React has seen it, and React
    // leaves an attribute it doesn't know alone when it says so here.
    <html lang="en" className="h-full antialiased" suppressHydrationWarning>
      <body className="flex min-h-full flex-col">
        {/* Runs while the page is read, before its first paint: whether the list of
            conversations has a place (lib/frame.ts). A constant written here.

            At the start of the body, not in the head: the parser waits here until the
            stylesheet is in, as it would there, but it has read the whole head by
            then, and the browser asks for the page's scripts at once. In the head,
            after the stylesheet, Chromium asked for one of them at a time (ADR 0015,
            revision of 2026-11-22). And it still runs before anything of the frame
            has been read, so the mark is there before the frame's first paint. */}
        <script dangerouslySetInnerHTML={{ __html: LIST_AWAY_SCRIPT }} />
        {children}
      </body>
    </html>
  );
}

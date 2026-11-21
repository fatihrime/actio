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
      <head>
        {/* Runs while the page is read, before its first paint: whether the list of
            conversations has a place (lib/frame.ts). A constant written here. */}
        <script dangerouslySetInnerHTML={{ __html: LIST_AWAY_SCRIPT }} />
      </head>
      <body className="flex min-h-full flex-col">{children}</body>
    </html>
  );
}

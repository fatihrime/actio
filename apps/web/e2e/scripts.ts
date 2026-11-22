// The app's own scripts, as the build serves them and a page names them: Next.js serves
// them under `/_next/static/`, under names that are the build's own.
import type { Request } from "@playwright/test";

/** Whether `request` asks for one of the app's own scripts. */
export function isAppScript(request: Request): boolean {
  return (
    request.resourceType() === "script" &&
    new URL(request.url()).pathname.startsWith("/_next/static/")
  );
}

/** The paths of the app's scripts that a page's HTML names itself, in its `<script
 * src>`: those a browser of today runs, so not the one marked `nomodule`, which only a
 * browser without modules asks for. */
export function scriptsNamedIn(html: string): string[] {
  return [...html.matchAll(/<script\b[^>]*>/gi)].flatMap(([tag]) => {
    const path = /\ssrc="(\/_next\/static\/[^"?#]+)/i.exec(tag)?.[1];
    return path === undefined || /\snomodule\b/i.test(tag) ? [] : [path];
  });
}

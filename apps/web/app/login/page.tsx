import type { Metadata } from "next";

import { ChatApp } from "@/components/chat/chat-app";

export const metadata: Metadata = { title: "Log in · Actio" };

/** Where to log in. The form is part of the app's one screen, which reads from the
 * address what to show: see `ChatApp`. */
export default function LogInPage() {
  return <ChatApp />;
}

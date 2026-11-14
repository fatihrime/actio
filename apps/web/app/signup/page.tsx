import type { Metadata } from "next";

import { ChatApp } from "@/components/chat/chat-app";

export const metadata: Metadata = { title: "Sign up · Actio" };

/** Where to make a lasting account. The form is part of the app's one screen, which
 * reads from the address what to show: see `ChatApp`. */
export default function SignUpPage() {
  return <ChatApp />;
}

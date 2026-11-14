import type { Metadata } from "next";

import { ChatApp } from "@/components/chat/chat-app";

export const metadata: Metadata = { title: "Your account · Actio" };

/** Where an account's owner changes the password and logs out. Part of the app's one
 * screen, which reads from the address what to show: see `ChatApp`. */
export default function AccountPage() {
  return <ChatApp />;
}

import { ChatApp } from "@/components/chat/chat-app";

/** A stored conversation. Which one is read from the address in the browser, by the
 * same component the home page shows: see `ChatApp`. */
export default function ConversationPage() {
  return <ChatApp />;
}

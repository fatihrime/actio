// The worker that colours code, off the page's thread: lib/chat/code-colours.ts starts
// it at the first block of code a page shows, and asks it about each block.
import { colour } from "./colouring";
import type { Answered, Asked } from "./colouring";

/** What this worker uses of its global scope. The app's types are the page's, and a
 * worker's scope is not a page's window. */
interface Scope {
  onmessage: ((event: MessageEvent<Asked>) => void) | null;
  postMessage: (message: Answered) => void;
}

const scope = self as unknown as Scope;

scope.onmessage = ({ data: { id, question } }) => {
  colour(question).then(
    (colours) => {
      scope.postMessage({ id, colours });
    },
    (error: unknown) => {
      scope.postMessage({
        id,
        error: error instanceof Error ? error.message : String(error),
      });
    },
  );
};

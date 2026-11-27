// A model loaded ahead of the first question to it (ADR 0026).
//
// A local model's server unloads a model a few minutes after it was last asked
// anything, and the next question then waits seconds for the load before its first
// word. So the page asks the API to have the model that answers loaded: once the page
// knows that model and it is ready, and when something is written into the box, which
// is when someone is about to ask. The API answers at once, and the load goes on at the
// server. A question asked meanwhile waits for it there, as it would have waited for
// its own.
//
// Asked often, it is asked at most once a minute for one session and model. The time
// is kept here, in the module, and not in a component: React's StrictMode runs an
// effect twice, and a component that appears again would ask again.
import { warmUpModel } from "@/lib/api/client";
import type { Model } from "@/lib/api/client";

/** How long after asking the page doesn't ask again for the same session and model,
 * whatever came of it. A local server keeps a model about five minutes after the last
 * request, and each load ahead moves that time on. */
export const WARM_UP_EVERY_MS = 60_000;

/** When each session and model was last asked for, by the clock: keyed by both. */
const asked = new Map<string, number>();

/** Ask for `model` to be loaded, unless it isn't ready, or it was asked for in this
 * session within the last `WARM_UP_EVERY_MS`. `holder` is what the session's data is
 * held under (`Session.holder`): a new session asks anew.
 *
 * It says nothing of how that went. A refusal, or a request that fails, changes
 * nothing on the page: nobody asked for this, and the question will be asked anyway.
 *
 * It reads the model's id and its status, and nothing else the list says of it. */
export function warmUp(
  holder: string | null,
  model: Pick<Model, "id" | "status"> | null,
): void {
  if (holder === null || model?.status !== "ready") {
    return;
  }
  const key = JSON.stringify([holder, model.id]);
  const now = Date.now();
  const last = asked.get(key);
  // Either way round: a clock that was set back by more than that asks again at once,
  // and not only once it has caught up with the last time it asked.
  if (last !== undefined && Math.abs(now - last) < WARM_UP_EVERY_MS) {
    return;
  }
  asked.set(key, now);
  warmUpModel(model.id).catch(() => undefined);
}

import { describe, expect, it, vi } from "vitest";

import { problem } from "@/tests/fake-api";

import { ApiProblem, fetchStream, turnsUrl } from "./client";

describe("ApiProblem", () => {
  it("carries the problem document the API answered with", async () => {
    const refused = await ApiProblem.from(
      problem(429, "Too many messages. Try again in 40 seconds.", {
        retry_after: 40,
      }),
    );

    expect(refused).toBeInstanceOf(Error);
    expect(refused.message).toBe("Too many messages. Try again in 40 seconds.");
    expect(refused.status).toBe(429);
    expect(refused.retryAfter).toBe(40);
    expect(refused.problem).toMatchObject({ type: "about:blank", status: 429 });
  });

  it("makes do when the answer isn't a problem document", async () => {
    // What a proxy says while the API behind it is down.
    const refused = await ApiProblem.from(
      new Response("<html>Bad Gateway</html>", { status: 502 }),
    );

    expect(refused.status).toBe(502);
    expect(refused.message).toBe("The server didn't answer as expected (502).");
    expect(refused.retryAfter).toBeUndefined();
  });

  it("goes by the status of the response, not by what its body claims", async () => {
    const refused = await ApiProblem.from(
      new Response(JSON.stringify({ detail: "Gone wrong.", status: 200 }), {
        status: 500,
      }),
    );

    expect(refused.status).toBe(500);
    expect(refused.message).toBe("Gone wrong.");
  });
});

describe("fetchStream", () => {
  it("hands on an answer that is being streamed", async () => {
    const streaming = new Response("data: [DONE]\n\n");
    vi.stubGlobal("fetch", () => Promise.resolve(streaming));

    expect(await fetchStream(turnsUrl("c1"), { method: "POST" })).toBe(
      streaming,
    );
  });

  it("throws a refusal as the problem it is", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(
        problem(409, "This conversation is still being answered."),
      ),
    );

    await expect(fetchStream(turnsUrl("c1"))).rejects.toMatchObject({
      name: "ApiProblem",
      status: 409,
      message: "This conversation is still being answered.",
    });
  });
});

describe("turnsUrl", () => {
  it("is where a conversation's turns are posted", () => {
    expect(turnsUrl("01900000-0000-7000-8000-0000000000c1")).toBe(
      "/api/conversations/01900000-0000-7000-8000-0000000000c1/turns",
    );
  });
});

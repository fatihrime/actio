// Ids made in the browser, for conversations and messages.
//
// The browser names a conversation and a question before the server has heard of them,
// so a page can show them at once and a repeated request can't ask twice. The API
// accepts any UUID. These are version 7, like the ones the server makes: they begin
// with the time, so rows made around the same moment sit together in the database.

/** A new UUID, version 7 (RFC 9562): 48 bits of time, then random bits. */
export function newId(now: number = Date.now()): string {
  // Not crypto.randomUUID(): browsers offer it only to pages served over HTTPS or
  // from localhost, and Actio may be reached over plain HTTP in a home network.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let time = now;
  for (let index = 5; index >= 0; index--) {
    bytes[index] = time % 256;
    time = Math.floor(time / 256);
  }
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x70; // version 7
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80; // the RFC's variant
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10).join(""),
  ].join("-");
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Whether `text` has the form of a UUID. */
export function isId(text: string): boolean {
  return UUID.test(text);
}

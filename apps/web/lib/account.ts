// What the API asks of an account's email address and password, and of the name of an
// API key, so a form can say it before anything is sent. The API has the last word:
// these only save a round trip. A test holds the numbers to the API's own
// (tests/contract/limits.test.ts).

/** The longest email address the API takes. */
export const EMAIL_MAX = 254;
/** A new password's length, in characters (ADR 0010): no other rule. */
export const PASSWORD_MIN = 10;
export const PASSWORD_MAX = 256;
/** An API key's name's length, in characters, without the spaces around it (ADR 0033):
 * no other rule. */
export const KEY_NAME_MIN = 1;
export const KEY_NAME_MAX = 64;

/** What is wrong with an email address, in words for its owner. Nothing if it has the
 * form of one. Whether it is anyone's, only the API can say. */
export function emailProblem(email: string): string | null {
  if (email === "") {
    return "Enter your email address.";
  }
  if (!/^[^@\s]+@[^@\s]+$/.test(email) || email.length > EMAIL_MAX) {
    return "This doesn’t look like an email address.";
  }
  return null;
}

/** What is wrong with a password that is being chosen. Nothing if the API will take
 * it. */
export function newPasswordProblem(password: string): string | null {
  // Counted as the API counts, by code point: "é" is one character here, and so is a
  // plain emoji, which JavaScript's `length` would count as two.
  const length = Array.from(password).length;
  if (length < PASSWORD_MIN) {
    return `Use at least ${String(PASSWORD_MIN)} characters.`;
  }
  if (length > PASSWORD_MAX) {
    return `Use at most ${String(PASSWORD_MAX)} characters.`;
  }
  return null;
}

/** What is wrong with a name for an API key, as it is sent: without the spaces around
 * it. Nothing if the API will take it. */
export function keyNameProblem(name: string): string | null {
  // By code point, as the API counts, and as a new password is counted above.
  const length = Array.from(name).length;
  if (length < KEY_NAME_MIN) {
    return "Give the key a name.";
  }
  if (length > KEY_NAME_MAX) {
    return `Use ${String(KEY_NAME_MIN)} to ${String(KEY_NAME_MAX)} characters.`;
  }
  return null;
}

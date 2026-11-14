import { describe, expect, it } from "vitest";

import { emailProblem, newPasswordProblem } from "./account";

describe("emailProblem", () => {
  it("finds nothing wrong with what has the form of an address", () => {
    expect(emailProblem("ada@example.com")).toBeNull();
    expect(emailProblem("ada.lovelace+actio@mail.example.org")).toBeNull();
    // On a home network: the API decides whether it takes such a host.
    expect(emailProblem("ada@nas")).toBeNull();
  });

  it("asks for an address when there is none", () => {
    expect(emailProblem("")).toBe("Enter your email address.");
  });

  it.each(["ada", "ada@", "@example.com", "ada lovelace@example.com", "a@b@c"])(
    "says that %j doesn't look like one",
    (typed) => {
      expect(emailProblem(typed)).toBe(
        "This doesn’t look like an email address.",
      );
    },
  );

  it("refuses an address longer than the API takes", () => {
    const host = "@example.com";
    expect(emailProblem("a".repeat(254 - host.length) + host)).toBeNull();
    expect(emailProblem("a".repeat(255 - host.length) + host)).not.toBeNull();
  });
});

describe("newPasswordProblem", () => {
  it("asks for 10 to 256 characters, and nothing else", () => {
    expect(newPasswordProblem("123456789")).toBe("Use at least 10 characters.");
    expect(newPasswordProblem("1234567890")).toBeNull();
    expect(newPasswordProblem("aaaaaaaaaa")).toBeNull(); // no rule about variety
    expect(newPasswordProblem("a".repeat(256))).toBeNull();
    expect(newPasswordProblem("a".repeat(257))).toBe(
      "Use at most 256 characters.",
    );
  });

  it("counts characters as the API does, not as JavaScript stores them", () => {
    // Nine letters and one emoji: ten characters to the API, eleven to `length`.
    const withEmoji = "abcdefghi🚲";
    expect(withEmoji).toHaveLength(11);
    expect(newPasswordProblem(withEmoji)).toBeNull();
    // Five emoji are five characters, though `length` says ten.
    expect(newPasswordProblem("🚲🚲🚲🚲🚲")).toBe(
      "Use at least 10 characters.",
    );
  });
});

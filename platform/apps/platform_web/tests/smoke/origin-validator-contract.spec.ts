import { expect, test } from "@playwright/test";
import { validateLiveQaOrigin } from "../support/live-qa-origin";

test("credential-bearing live QA accepts only the exact HTTPS production origin", () => {
  expect(validateLiveQaOrigin({
    allowLoopback: false,
    configured: "https://old-sparky.com/",
    expected: "https://old-sparky.com",
  })).toBe("https://old-sparky.com");
  expect(() => validateLiveQaOrigin({
    allowLoopback: false,
    configured: "https://lookalike.example",
    expected: "https://old-sparky.com",
  })).toThrow(/does not match/u);
  for (const configured of [
    "http://old-sparky.com",
    "https://user:secret@old-sparky.com",
    "https://old-sparky.com/path",
    "https://old-sparky.com/?token=secret",
    "https://old-sparky.com/#fragment",
  ]) {
    expect(() => validateLiveQaOrigin({
      allowLoopback: false,
      configured,
      expected: "https://old-sparky.com",
    })).toThrow();
  }
  expect(() => validateLiveQaOrigin({
    allowLoopback: false,
    configured: "http://127.0.0.1:3100",
    expected: "http://127.0.0.1:3100",
  })).toThrow(/loopback/u);
  expect(() => validateLiveQaOrigin({
    allowLoopback: false,
    configured: "https://attacker.example",
    expected: "https://attacker.example",
  })).toThrow(/old-sparky\.com/u);
  expect(() => validateLiveQaOrigin({
    allowLoopback: true,
    configured: "https://attacker.example",
    expected: "https://attacker.example",
  })).toThrow(/loopback/u);
  expect(validateLiveQaOrigin({
    allowLoopback: true,
    configured: "http://127.0.0.1:3100",
    expected: "http://127.0.0.1:3100",
  })).toBe("http://127.0.0.1:3100");
});

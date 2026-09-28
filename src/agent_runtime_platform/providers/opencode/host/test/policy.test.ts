import { describe, expect, test } from "bun:test";

import {
  DENIED_ACTIONS,
  TOOL_POLICY,
  assertToolPolicy,
  whollyDenied,
  type PermissionRule,
} from "../src/policy";

describe("the tool policy", () => {
  test("denies the named dangerous tools, then everything else", () => {
    for (const action of ["shell", "edit", "write", "patch", "webfetch", "websearch"]) {
      expect(whollyDenied(action, TOOL_POLICY)).toBe(true);
    }
    expect(TOOL_POLICY.at(-1)).toEqual({ action: "*", resource: "*", effect: "deny" });
  });

  test("names the actions the re-check must find denied", () => {
    expect(DENIED_ACTIONS).toContain("shell");
    expect(DENIED_ACTIONS).toContain("edit");
    expect(DENIED_ACTIONS).toContain("webfetch");
    expect(DENIED_ACTIONS).toContain("websearch");
  });
});

describe("whollyDenied", () => {
  test("is true only when the last matching rule denies every resource", () => {
    const deny: PermissionRule = { action: "shell", resource: "*", effect: "deny" };
    const allow: PermissionRule = { action: "shell", resource: "*", effect: "allow" };
    const narrow: PermissionRule = { action: "shell", resource: "git *", effect: "deny" };

    expect(whollyDenied("shell", [deny])).toBe(true);
    expect(whollyDenied("shell", [allow])).toBe(false);
    expect(whollyDenied("shell", [deny, allow])).toBe(false);
    expect(whollyDenied("shell", [allow, deny])).toBe(true);
    expect(whollyDenied("shell", [narrow])).toBe(false);
    expect(whollyDenied("edit", [deny])).toBe(false);
    expect(whollyDenied("shell", [])).toBe(false);
  });

  test("matches whole-value wildcards", () => {
    expect(whollyDenied("shell", [{ action: "*", resource: "*", effect: "deny" }])).toBe(true);
    expect(whollyDenied("shell", [{ action: "shel?", resource: "*", effect: "deny" }])).toBe(true);
    expect(whollyDenied("shell", [{ action: "s*", resource: "*", effect: "deny" }])).toBe(true);
    expect(whollyDenied("shell", [{ action: "edit*", resource: "*", effect: "deny" }])).toBe(false);
  });
});

describe("assertToolPolicy", () => {
  test("accepts the policy this host runs with", () => {
    expect(() => assertToolPolicy(TOOL_POLICY)).not.toThrow();
  });

  test("re-checks the rules it is given, not the constant", () => {
    const relaxed: PermissionRule[] = [
      ...TOOL_POLICY,
      { action: "shell", resource: "*", effect: "allow" },
    ];
    expect(() => assertToolPolicy(relaxed)).toThrow("shell");
  });

  test("reports every action that is not denied", () => {
    const withoutShell: PermissionRule[] = TOOL_POLICY.filter(
      (rule) => rule.action !== "shell" && rule.action !== "*",
    );
    try {
      assertToolPolicy(withoutShell);
      throw new Error("expected a refusal");
    } catch (error) {
      expect(String(error)).toContain("shell");
    }
  });

  test("an empty rule set denies nothing", () => {
    expect(() => assertToolPolicy([])).toThrow();
  });
});

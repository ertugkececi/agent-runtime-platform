import { describe, expect, test } from "bun:test";

import {
  DENIED_ACTIONS,
  assertToolPolicy,
  mcpToolAction,
  serverToolActions,
  toolPolicy,
  whollyAllowed,
  whollyDenied,
  type PermissionRule,
} from "../src/policy";

const EVERYTHING_DENIED = toolPolicy([]);
const ONE_GRANT = toolPolicy(["files_lookup"]);

describe("the tool policy", () => {
  test("without grants, denies the named dangerous tools, then everything else", () => {
    for (const action of ["shell", "edit", "write", "patch", "webfetch", "websearch"]) {
      expect(whollyDenied(action, EVERYTHING_DENIED)).toBe(true);
    }
    expect(EVERYTHING_DENIED.at(-1)).toEqual({ action: "*", resource: "*", effect: "deny" });
  });

  test("with a grant, allows exactly that action and denies the rest", () => {
    expect(whollyAllowed("files_lookup", ONE_GRANT)).toBe(true);
    expect(whollyDenied("files_lookup", ONE_GRANT)).toBe(false);
    expect(whollyDenied("shell", ONE_GRANT)).toBe(true);
    expect(whollyDenied("files_delete", ONE_GRANT)).toBe(true);
    // The allow is the last rule, because the last matching rule wins.
    expect(ONE_GRANT.at(-1)).toEqual({ action: "files_lookup", resource: "*", effect: "allow" });
    expect(ONE_GRANT.at(-2)).toEqual({ action: "*", resource: "*", effect: "deny" });
  });

  test("names the actions the re-check must find denied", () => {
    expect(DENIED_ACTIONS).toContain("shell");
    expect(DENIED_ACTIONS).toContain("edit");
    expect(DENIED_ACTIONS).toContain("webfetch");
    expect(DENIED_ACTIONS).toContain("websearch");
  });
});

describe("MCP tool actions", () => {
  test("mirrors OpenCode's effective-name rule for MCP tools", () => {
    expect(mcpToolAction("files", "lookup")).toBe("files_lookup");
    expect(mcpToolAction("my-server", "read.file")).toBe("my-server_read_file");
    expect(mcpToolAction("repo.tools", "search/issues")).toBe("repo_tools_search_issues");
  });

  test("builds one action per server", () => {
    expect(serverToolActions("files", ["lookup", "read.file"])).toEqual([
      "files_lookup",
      "files_read_file",
    ]);
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
    expect(() => assertToolPolicy(EVERYTHING_DENIED, [])).not.toThrow();
    expect(() => assertToolPolicy(ONE_GRANT, ["files_lookup"])).not.toThrow();
  });

  test("re-checks the rules it is given, not the constant", () => {
    const relaxed: PermissionRule[] = [
      ...EVERYTHING_DENIED,
      { action: "shell", resource: "*", effect: "allow" },
    ];
    expect(() => assertToolPolicy(relaxed, [])).toThrow("shell");
  });

  test("reports every action that is not denied", () => {
    const withoutShell: PermissionRule[] = EVERYTHING_DENIED.filter(
      (rule) => rule.action !== "shell" && rule.action !== "*",
    );
    try {
      assertToolPolicy(withoutShell, []);
      throw new Error("expected a refusal");
    } catch (error) {
      expect(String(error)).toContain("shell");
    }
  });

  test("refuses when a granted tool is not allowed", () => {
    expect(() => assertToolPolicy(EVERYTHING_DENIED, ["files_lookup"])).toThrow("files_lookup");
  });

  test("an empty rule set denies nothing", () => {
    expect(() => assertToolPolicy([], [])).toThrow();
  });
});

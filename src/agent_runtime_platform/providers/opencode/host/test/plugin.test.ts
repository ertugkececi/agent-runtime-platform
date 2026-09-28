import { describe, expect, test } from "bun:test";

import { Plugin } from "@opencode/plugin";

import { createToolPolicyPlugin, appendToolPolicy, policyHook } from "../src/plugin";
import { POLICY_PLUGIN_ID, type PermissionRule } from "../src/policy";

interface FakeAgent {
  id: string;
  permissions: PermissionRule[];
}

function fakeContext() {
  const agents: FakeAgent[] = [
    { id: "build", permissions: [{ action: "*", resource: "*", effect: "allow" }] },
    { id: "explore", permissions: [] },
  ];
  const transforms: ((editor: {
    list(): readonly FakeAgent[];
    update(id: string, update: (agent: FakeAgent) => void): void;
  }) => void)[] = [];
  const hooks: { name: string; callback: (event: { tool: string }) => void }[] = [];
  const catalog = [
    { id: "shell" },
    { id: "files_search", options: { namespace: "files" } },
  ];
  const context = {
    agent: {
      transform: async (callback: (typeof transforms)[number]) => {
        transforms.push(callback);
      },
    },
    tool: {
      list: async () => catalog,
      hook: async (name: string, callback: (event: { tool: string }) => void) => {
        hooks.push({ name, callback });
      },
    },
  };
  return { context, agents, transforms, hooks, catalog };
}

describe("the tool policy plugin", () => {
  test("registers under a stable id", () => {
    const { plugin } = createToolPolicyPlugin();
    expect(plugin.id).toBe(POLICY_PLUGIN_ID);
  });

  test("captures the registered tool catalog", async () => {
    const fake = fakeContext();
    const { plugin, catalog } = createToolPolicyPlugin();
    await plugin.setup(fake.context as unknown as Plugin.Context);
    expect(catalog()).toEqual(fake.catalog);
  });

  test("appends the policy to every agent", async () => {
    const fake = fakeContext();
    const { plugin } = createToolPolicyPlugin();
    await plugin.setup(fake.context as unknown as Plugin.Context);

    const transform = fake.transforms[0];
    expect(transform).toBeDefined();
    transform?.({
      list: () => fake.agents,
      update: (id, update) => {
        const agent = fake.agents.find((candidate) => candidate.id === id);
        expect(agent).toBeDefined();
        if (agent) update(agent);
      },
    });

    for (const agent of fake.agents) {
      expect(agent.permissions.at(-1)).toEqual({
        action: "*",
        resource: "*",
        effect: "deny",
      });
    }
  });

  test("hooks tool execution so a denied tool cannot run", async () => {
    const fake = fakeContext();
    const { plugin } = createToolPolicyPlugin();
    await plugin.setup(fake.context as unknown as Plugin.Context);

    const hook = fake.hooks.find((candidate) => candidate.name === "execute.before");
    expect(hook).toBeDefined();
    expect(() => hook?.callback({ tool: "shell" })).toThrow("not permitted");
    expect(() => hook?.callback({ tool: "some_other_tool" })).toThrow("not permitted");
  });
});

describe("policy helpers", () => {
  test("appendToolPolicy adds the rules to the end", () => {
    const permissions: PermissionRule[] = [{ action: "shell", resource: "*", effect: "allow" }];
    appendToolPolicy(permissions);
    expect(permissions.at(-1)).toEqual({ action: "*", resource: "*", effect: "deny" });
    expect(permissions.length).toBeGreaterThan(1);
  });

  test("policyHook refuses any tool the policy denies", () => {
    expect(() => policyHook({ tool: "edit" })).toThrow();
  });
});

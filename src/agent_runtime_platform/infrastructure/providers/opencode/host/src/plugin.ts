/**
 * The plugin that carries the tool policy into OpenCode.
 *
 * Permissions alone decide what the model's catalog offers, but the policy has
 * to survive every other rule source: an agent can append its own rules after
 * the global ones. The plugin therefore appends the policy to *every* agent,
 * as the last rules of the last rule source, and hooks tool execution so a
 * denied tool cannot run even if its definition leaked into a catalog.
 *
 * The plugin also captures the registered tool catalog, which the trace mapper
 * uses to split a tool id into its server and tool names.
 */

import { Plugin } from "@opencode/plugin";

import { POLICY_PLUGIN_ID, TOOL_POLICY, whollyDenied, type PermissionRule } from "./policy";
import type { ToolDescriptor } from "./trace";

export interface ToolPolicyPlugin {
  readonly plugin: Plugin.Plugin;
  /** The tool catalog as registered, after every transform. */
  readonly catalog: () => readonly ToolDescriptor[];
}

export function createToolPolicyPlugin(): ToolPolicyPlugin {
  let catalog: readonly ToolDescriptor[] = [];
  const plugin = Plugin.define({
    id: POLICY_PLUGIN_ID,
    setup: async (ctx) => {
      catalog = await ctx.tool.list();
      await ctx.agent.transform((editor) => {
        for (const agent of editor.list()) {
          // The editor's deep-mutable view turns the branded agent id into an
          // object type; at runtime it is a string.
          editor.update(String(agent.id), (item) => {
            appendToolPolicy(item.permissions);
          });
        }
      });
      await ctx.tool.hook("execute.before", policyHook);
    },
  });
  return { plugin, catalog: () => catalog };
}

/** Append the policy as the last rules of an agent's rule list. */
export function appendToolPolicy(permissions: PermissionRule[]): void {
  permissions.push(...TOOL_POLICY);
}

/** Refuse a tool call that the policy denies. The turn fails, as it must. */
export function policyHook(event: { tool: string }): void {
  if (whollyDenied(event.tool, TOOL_POLICY)) {
    throw new Error(`Tool '${event.tool}' is not permitted by the bridge host policy.`);
  }
}

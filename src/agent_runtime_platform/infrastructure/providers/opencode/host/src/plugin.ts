/**
 * The plugin that carries the tool policy into OpenCode.
 *
 * Permissions alone decide what the model's catalog offers, but the policy has
 * to survive every other rule source: an agent can append its own rules after
 * the global ones. The plugin therefore appends the policy to *every* agent,
 * as the last rules of the last rule source, and hooks tool execution so a
 * tool outside the grant list cannot run even if its definition leaked into a
 * catalog.
 *
 * The plugin also captures the registered tool catalog, which the trace mapper
 * uses to split a tool id into its server and tool names, and which the caller
 * re-reads until the granted MCP tools are registered.
 */

import { Plugin } from "@opencode/plugin";

import { POLICY_PLUGIN_ID, policyHook, toolPolicy, type PermissionRule } from "./policy";
import type { ToolDescriptor } from "./trace";

export interface ToolPolicyPlugin {
  readonly plugin: Plugin.Plugin;
  /** The tool catalog as registered, after every transform. */
  readonly catalog: () => readonly ToolDescriptor[];
  /** Re-read the registered tool catalog; a fresh host may still be connecting. */
  readonly refresh: () => Promise<readonly ToolDescriptor[]>;
}

export function createToolPolicyPlugin(allowedActions: readonly string[]): ToolPolicyPlugin {
  const policy = toolPolicy(allowedActions);
  let catalog: readonly ToolDescriptor[] = [];
  let reload: (() => Promise<readonly ToolDescriptor[]>) | undefined;
  const plugin = Plugin.define({
    id: POLICY_PLUGIN_ID,
    setup: async (ctx) => {
      const read = async () => {
        catalog = await ctx.tool.list();
        return catalog;
      };
      reload = read;
      await read();
      await ctx.agent.transform((editor) => {
        for (const agent of editor.list()) {
          // The editor's deep-mutable view turns the branded agent id into an
          // object type; at runtime it is a string.
          editor.update(String(agent.id), (item) => {
            appendToolPolicy(item.permissions, policy);
          });
        }
      });
      await ctx.tool.hook("execute.before", (event) => policyHook(event, allowedActions));
    },
  });
  return {
    plugin,
    catalog: () => catalog,
    refresh: async () => {
      // The plugin's setup has run before any caller asks; when it has not,
      // the empty catalog is the honest answer.
      return reload === undefined ? catalog : reload();
    },
  };
}

/** Append the policy as the last rules of an agent's rule list. */
export function appendToolPolicy(
  permissions: PermissionRule[],
  policy: readonly PermissionRule[],
): void {
  permissions.push(...policy);
}

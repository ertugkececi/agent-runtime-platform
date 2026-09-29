/**
 * The tool policy the bridge host runs with.
 *
 * The runtime only ever grants administrator-approved, read-only MCP tools, so
 * the host must expose exactly those tools and nothing else. OpenCode resolves
 * permissions by "the last matching rule wins", so the policy is built the way
 * OpenCode documents its own examples: the broad rules first, the specific
 * exceptions after them.
 *
 * - the named dangerous actions are denied first, so the policy reads as intent;
 * - then one catch-all `deny` closes every action it matches;
 * - then one `allow` rule per granted tool action, last, so a granted tool is
 *   the last match for its own action and every other action stays denied.
 *
 * `assertToolPolicy` re-reads the effective permissions and refuses to start a
 * turn when the policy is not in force - the check is behavioural, not a
 * comparison against this constant. The hook refuses any execution whose
 * action is not in the grant list, even if a definition leaked into a catalog.
 */

import { BridgeError } from "./protocol";

export interface PermissionRule {
  action: string;
  resource: string;
  effect: "allow" | "deny" | "ask";
}

/** The id of the plugin that carries this policy into every agent. */
export const POLICY_PLUGIN_ID = "agent-runtime.tool-policy";

/** The agent the bridge runs its session as. It carries the policy itself. */
export const BRIDGE_AGENT_ID = "bridge";

/**
 * Actions the host explicitly closes. The named dangerous ones come first so
 * the policy reads as intent; `write` and `patch` share the `edit` action.
 */
export const DENIED_ACTIONS: readonly string[] = [
  "shell",
  "edit",
  "read",
  "glob",
  "grep",
  "webfetch",
  "websearch",
  "subagent",
  "skill",
  "question",
  "external_directory",
  "execute",
];

/**
 * The policy for one turn: the granted tool actions allowed, everything else
 * denied. The allows come last, so a granted tool is the last match for its
 * own action; the catch-all closes every other action and keeps the model's
 * catalog free of everything but the grants.
 */
export function toolPolicy(allowedActions: readonly string[]): readonly PermissionRule[] {
  const allowed = [...new Set(allowedActions)].filter((action) => action.length > 0).sort();
  return Object.freeze([
    ...DENIED_ACTIONS.map((action) => ({ action, resource: "*", effect: "deny" as const })),
    { action: "*", resource: "*", effect: "deny" as const },
    ...allowed.map((action) => ({ action, resource: "*", effect: "allow" as const })),
  ]);
}

/**
 * The permission action OpenCode derives for one MCP tool of one server:
 * `namespace_tool`, with everything outside `[a-zA-Z0-9_-]` replaced by `_`.
 * This mirrors OpenCode's own effective-name rule for MCP tools.
 */
export function mcpToolAction(server: string, tool: string): string {
  const sanitize = (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, "_");
  return `${sanitize(server)}_${sanitize(tool)}`;
}

/** The permission actions one server's granted tools resolve to. */
export function serverToolActions(server: string, tools: readonly string[]): string[] {
  return [...new Set(tools.map((tool) => mcpToolAction(server, tool)))].sort();
}

/**
 * Mirrors OpenCode's own "wholly disabled" rule: the last matching rule is a
 * deny for every resource. Matching uses OpenCode's whole-value wildcards.
 */
export function whollyDenied(action: string, rules: readonly PermissionRule[]): boolean {
  const rule = rules.findLast((candidate) => matches(candidate.action, action));
  return rule?.resource === "*" && rule.effect === "deny";
}

/** The last matching rule for an action is an allow for every resource. */
export function whollyAllowed(action: string, rules: readonly PermissionRule[]): boolean {
  const rule = rules.findLast((candidate) => matches(candidate.action, action));
  return rule?.resource === "*" && rule.effect === "allow";
}

/**
 * Re-check the effective permissions before any model work starts.
 *
 * Raises `BridgeError("provider", ...)` when a dangerous action is not wholly
 * denied or a granted tool is not wholly allowed, which the bridge reports as
 * an `error` record and turns into a failed turn.
 */
export function assertToolPolicy(
  rules: readonly PermissionRule[],
  allowedActions: readonly string[],
): void {
  const open = DENIED_ACTIONS.filter((action) => !whollyDenied(action, rules));
  if (open.length > 0) {
    throw new BridgeError(
      "provider",
      `The OpenCode tool policy is not in force: ${open.join(", ")}. Refusing to start the turn.`,
    );
  }
  const missing = allowedActions.filter((action) => !whollyAllowed(action, rules));
  if (missing.length > 0) {
    throw new BridgeError(
      "provider",
      `The OpenCode tool policy does not allow the granted tools: ${missing.join(", ")}. Refusing to start the turn.`,
    );
  }
}

/** Refuse a tool call that is not one of this turn's grants. */
export function policyHook(
  event: { tool: string },
  allowedActions: readonly string[],
): void {
  if (!allowedActions.includes(event.tool)) {
    throw new Error(`Tool '${event.tool}' is not permitted by the bridge host policy.`);
  }
}

/** Whole-value wildcard matching, `*` and `?`, as the permissions document it. */
function matches(pattern: string, value: string): boolean {
  if (pattern === value) return true;
  if (!pattern.includes("*") && !pattern.includes("?")) return false;
  const expression = pattern
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(`^${expression}$`).test(value);
}

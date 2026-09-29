/**
 * The tool policy the bridge host runs with.
 *
 * This provider refuses every tool grant in this slice (its manifest declares
 * `supports_tool_ids = false`), so the model must not see a tool at all. The
 * runtime refuses the grants with an explicit `422`; this package has to hold
 * the same line inside OpenCode.
 *
 * OpenCode hides a tool from the model's catalog when the last matching rule
 * for that tool's action is `deny` with resource `*`. The rules below are
 * appended after every other rule source, and the catch-all is last, so the
 * catalog is empty. `assertToolPolicy` re-reads the effective permissions and
 * refuses to start a turn when the policy is not in force - the check is
 * behavioural, not a comparison against this constant.
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
 * The policy: each action denied, then every other action denied too.
 *
 * The catch-all is what keeps the model's catalog empty rather than merely
 * removing the tools named above.
 */
export const TOOL_POLICY: readonly PermissionRule[] = Object.freeze([
  ...DENIED_ACTIONS.map((action) => ({ action, resource: "*", effect: "deny" as const })),
  { action: "*", resource: "*", effect: "deny" as const },
]);

/**
 * Mirrors OpenCode's own "wholly disabled" rule: the last matching rule is a
 * deny for every resource. Matching uses OpenCode's whole-value wildcards.
 */
export function whollyDenied(action: string, rules: readonly PermissionRule[]): boolean {
  const rule = rules.findLast((candidate) => matches(candidate.action, action));
  return rule?.resource === "*" && rule.effect === "deny";
}

/**
 * Re-check the effective permissions before any model work starts.
 *
 * Raises `BridgeError("provider", ...)` when an action is not wholly denied,
 * which the bridge reports as an `error` record and turns into a failed turn.
 */
export function assertToolPolicy(rules: readonly PermissionRule[]): void {
  const allowed = DENIED_ACTIONS.filter((action) => !whollyDenied(action, rules));
  if (allowed.length > 0) {
    throw new BridgeError(
      "provider",
      `The OpenCode tool policy is not in force: ${allowed.join(", ")}. Refusing to start the turn.`,
    );
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

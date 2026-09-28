/**
 * The process environment the host runs in.
 *
 * The bridge host must not inherit the invoking user's OpenCode configuration:
 * its config, data, cache and state roots are pointed at a private directory.
 * The adapter may provide its own private root through the environment (the
 * contract's isolation property); this only fills in what is not set.
 *
 * The variables have to be in place before the SDK is imported, because the
 * SDK resolves its global paths at import time. `index.ts` calls this before
 * it loads the runner, and so does every test that touches the SDK.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const XDG_VARIABLES: readonly (readonly [string, string])[] = [
  ["XDG_CONFIG_HOME", "config"],
  ["XDG_DATA_HOME", "data"],
  ["XDG_CACHE_HOME", "cache"],
  ["XDG_STATE_HOME", "state"],
];

export interface PrivateHome {
  /** The private root, or undefined when the environment already provides one. */
  readonly root: string | undefined;
  cleanup(): void;
}

/**
 * Point unset XDG roots at a private directory (mode 0700) and return it.
 *
 * When every variable is already set, nothing is created and `root` stays
 * undefined: the adapter's environment wins.
 */
export function preparePrivateHome(
  env: Record<string, string | undefined> = process.env,
): PrivateHome {
  const missing = XDG_VARIABLES.filter(([name]) => !env[name]?.trim());
  if (missing.length === 0) return { root: undefined, cleanup: () => {} };

  const root = mkdtempSync(join(tmpdir(), "agent-runtime-opencode-"));
  for (const [name, directory] of missing) {
    env[name] = join(root, directory);
  }
  return {
    root,
    cleanup() {
      rmSync(root, { recursive: true, force: true });
    },
  };
}

/**
 * The config root the host reads. The environment's private config root wins -
 * that is the mechanism the adapter uses - and a private directory under
 * `fallbackRoot` is used when there is none, so the invoking user's
 * `~/.config/opencode` is never read.
 */
export function configDirectory(
  env: Record<string, string | undefined> = process.env,
  fallbackRoot: string = tmpdir(),
): string {
  const configured = env["XDG_CONFIG_HOME"]?.trim();
  return configured ? join(configured, "opencode") : join(fallbackRoot, "opencode");
}

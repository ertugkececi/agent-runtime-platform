/**
 * The process environment the host runs in.
 *
 * The bridge host must not inherit the invoking user's OpenCode configuration:
 * its config, data, cache and state roots are pointed at a private directory.
 * The adapter provides a private home per call (the contract's isolation
 * property); this only fills in what the environment left unset.
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
 * Point the process's mode creation at private values: `0600` files and `0700`
 * directories, whatever umask the invoking shell had. The host calls this
 * before the SDK is imported, so every file the SDK creates - configuration,
 * cache, session data - stays private.
 */
export function applyPrivateUmask(): number {
  return process.umask(0o077);
}

/**
 * Send every console write to stderr; stdout is the bridge's record stream.
 *
 * SDK internals log through the console (the MCP spawner, for one), and a
 * single console line on stdout would corrupt the NDJSON stream. Stderr is
 * diagnostic only, as the contract says, so library diagnostics stay visible
 * without ever touching the protocol.
 */
export function redirectConsoleToStderr(): void {
  const write = (...values: readonly unknown[]) => {
    const text = values
      .map((value) => (typeof value === "string" ? value : inspect(value)))
      .join(" ");
    process.stderr.write(`${text}\n`);
  };
  for (const method of ["log", "info", "debug"] as const) {
    console[method] = write;
  }
}

function inspect(value: unknown): string {
  if (value instanceof Error) return String(value);
  try {
    return typeof value === "object" && value !== null ? JSON.stringify(value) : String(value);
  } catch {
    return String(value);
  }
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

/**
 * The bridge host entry point.
 *
 * Reads the first request object from stdin, writes NDJSON records on stdout,
 * and exits 0 only when a result record was written. A `code`-mode connection
 * reads one further line - the code the provider showed the human - while it
 * waits. The private environment is prepared before the SDK is imported,
 * because the SDK resolves its global paths when it loads.
 */

import { createInterface } from "node:readline";

import { applyPrivateUmask, preparePrivateHome, redirectConsoleToStderr } from "./isolation";
import { BridgeError } from "./protocol";

applyPrivateUmask();
redirectConsoleToStderr();
const home = preparePrivateHome();

let exitCode = 1;
try {
  const [{ runBridge }, { startTurnHost, listModels }, { listIntegrations, startConnect }] =
    await Promise.all([import("./bridge"), import("./session"), import("./connect")]);
  const reader = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const lines = reader[Symbol.asyncIterator]();
  const first = await lines.next();
  const input = first.done ? "" : first.value;
  exitCode = await runBridge(input, {
    createHost: startTurnHost,
    listModels,
    listIntegrations,
    connect: startConnect,
    readCode: async () => {
      const next = await lines.next();
      if (next.done) {
        throw new BridgeError("request", "The connection needs a code, but stdin closed.");
      }
      return parseCode(next.value);
    },
    write: (record) => {
      process.stdout.write(`${JSON.stringify(record)}\n`);
    },
  });
  reader.close();
} catch {
  // The host could not even start; the adapter still gets one error record.
  process.stdout.write(
    `${JSON.stringify({
      type: "error",
      kind: "provider",
      message: "The OpenCode bridge host failed to start.",
    })}\n`,
  );
} finally {
  home.cleanup();
}

process.exit(exitCode);

/** The one extra stdin line a `code`-mode connection reads: `{"code": "..."}`. */
function parseCode(line: string): string {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    throw new BridgeError("request", "The connection code line is not valid JSON.");
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new BridgeError("request", "The connection code line must be an object.");
  }
  const code = (value as Record<string, unknown>)["code"];
  if (typeof code !== "string" || !code.trim()) {
    throw new BridgeError("request", "The connection code line must carry a non-empty 'code'.");
  }
  return code;
}

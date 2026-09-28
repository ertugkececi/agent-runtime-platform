/**
 * The bridge host entry point.
 *
 * Reads the one request object on stdin, writes NDJSON records on stdout, and
 * exits 0 only when a result record was written. The private environment is
 * prepared before the SDK is imported, because the SDK resolves its global
 * paths when it loads.
 */

import { readFileSync } from "node:fs";

import { preparePrivateHome } from "./isolation";

const home = preparePrivateHome();

let exitCode = 1;
try {
  const [{ runBridge }, { startTurnHost }] = await Promise.all([
    import("./bridge"),
    import("./session"),
  ]);
  const input = readFileSync(0, "utf8");
  exitCode = await runBridge(input, {
    createHost: startTurnHost,
    write: (record) => {
      process.stdout.write(`${JSON.stringify(record)}\n`);
    },
  });
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

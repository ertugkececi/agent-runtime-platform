/**
 * The one test that starts the real embedded host.
 *
 * It creates a host and a session, then re-checks the effective tool policy
 * exactly as a turn does. It never prompts, so it reaches no model service.
 * The private environment must be in place before the SDK is imported, so the
 * runner is loaded dynamically after `preparePrivateHome`.
 */

import { afterAll, beforeAll, describe, expect, test } from "bun:test";

import { preparePrivateHome } from "../src/isolation";
import { DENIED_ACTIONS } from "../src/policy";

// The bundled model catalog is pinned by the SDK version, so this reference
// resolves without a network fetch. The test never prompts, so it needs no
// credential and makes no inference call.
const MODEL = "opencode/space-bunny-free";

let startTurnHost: typeof import("../src/session").startTurnHost;
let home: ReturnType<typeof preparePrivateHome>;

beforeAll(async () => {
  home = preparePrivateHome();
  ({ startTurnHost } = await import("../src/session"));
});

afterAll(() => {
  home.cleanup();
});

describe("the embedded host", () => {
  test("reports the tool policy in force before a turn", async () => {
    const host = await startTurnHost({ model: MODEL, system: "You are a test agent." });
    try {
      await expect(host.verifyPolicy()).resolves.toBeUndefined();
    } finally {
      await host.close();
    }
  }, 60_000);

  test("the policy denies every action the host lists", async () => {
    const host = await startTurnHost({ model: MODEL, system: "You are a test agent." });
    try {
      // The check above passing means each of these actions was wholly denied
      // by the effective rules of the agent the session runs as.
      expect(DENIED_ACTIONS.length).toBeGreaterThan(0);
      await host.verifyPolicy();
    } finally {
      await host.close();
    }
  }, 60_000);
});

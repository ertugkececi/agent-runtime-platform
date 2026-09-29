/**
 * The tests that start the real embedded host.
 *
 * They create a host and a session, then re-check the effective tool policy
 * exactly as a turn does. They never prompt, so they reach no model service.
 * The MCP grant test starts the fixture in `test/fixtures/`, not a real
 * server. The private environment must be in place before the SDK is imported,
 * so the runner is loaded dynamically after `preparePrivateHome`.
 */

import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { existsSync } from "node:fs";
import { join } from "node:path";

import { preparePrivateHome } from "../src/isolation";
import { DENIED_ACTIONS } from "../src/policy";

// The bundled model catalog is pinned by the SDK version, so this reference
// resolves without a network fetch. The tests never prompt, so they need no
// credential and make no inference call.
const MODEL = "opencode/space-bunny-free";
const FIXTURE = join(import.meta.dir, "fixtures", "readonly_mcp.ts");

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
    const host = await startTurnHost({
      model: MODEL,
      system: "You are a test agent.",
      mcpServers: [],
    });
    try {
      await expect(host.verifyPolicy()).resolves.toBeUndefined();
    } finally {
      await host.close();
    }
  }, 60_000);

  test("the policy denies every action the host lists", async () => {
    const host = await startTurnHost({
      model: MODEL,
      system: "You are a test agent.",
      mcpServers: [],
    });
    try {
      // The check above passing means each of these actions was wholly denied
      // by the effective rules of the agent the session runs as.
      expect(DENIED_ACTIONS.length).toBeGreaterThan(0);
      await host.verifyPolicy();
    } finally {
      await host.close();
    }
  }, 60_000);

  test("registers a granted MCP tool and allows exactly that tool", async () => {
    const host = await startTurnHost({
      model: MODEL,
      system: "You are a test agent.",
      mcpServers: [
        {
          name: "fixture",
          command: process.execPath,
          args: [FIXTURE],
          env_vars: [],
          tools: ["lookup"],
        },
      ],
    });
    try {
      // `startTurnHost` waits for the grant before it returns; the policy
      // check proves the allow rule survives every other rule source.
      await expect(host.verifyPolicy()).resolves.toBeUndefined();
    } finally {
      await host.close();
    }
  }, 60_000);

  test("keeps its database on disk under the data root", async () => {
    const dataHome = process.env["XDG_DATA_HOME"];
    expect(dataHome).toBeDefined();
    const databasePath = join(dataHome as string, "opencode", "opencode.db");
    const host = await startTurnHost({
      model: MODEL,
      system: "You are a test agent.",
      mcpServers: [],
    });
    try {
      // The embedded SDK would default to an in-memory database; the bridge
      // must keep the file so a provider sign-in survives the process.
      expect(existsSync(databasePath)).toBe(true);
    } finally {
      await host.close();
    }
    expect(existsSync(databasePath)).toBe(true);
  }, 60_000);
});

import { describe, expect, test } from "bun:test";

import {
  runBridge,
  type BridgeDependencies,
  type ConnectionHooks,
  type TurnHost,
} from "../src/bridge";
import { BRIDGE_PROTOCOL, BridgeError, type BridgeRecord } from "../src/protocol";
import type { TraceEvent } from "../src/trace";

function request(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    bridge_protocol: BRIDGE_PROTOCOL,
    model: "opencode/big-model",
    instructions: "Be helpful.",
    history: [{ role: "user", content: "Hello." }],
    allow_handoff: false,
    tool_ids: [],
    ...overrides,
  };
}

function fakeHost(overrides: Partial<TurnHost> = {}): TurnHost {
  return {
    async verifyPolicy() {},
    async prompt() {
      return "Hi.";
    },
    async close() {},
    ...overrides,
  };
}

async function run(
  input: unknown,
  overrides: Partial<BridgeDependencies> = {},
): Promise<{ records: BridgeRecord[]; code: number }> {
  const records: BridgeRecord[] = [];
  const dependencies: BridgeDependencies = {
    async createHost() {
      return fakeHost();
    },
    async listModels() {
      return [];
    },
    async listIntegrations() {
      return [];
    },
    async connect() {
      return { integration: "openai", method: "chatgpt-headless" };
    },
    async readCode() {
      return "the-code";
    },
    write: (record) => {
      records.push(record);
    },
    ...overrides,
  };
  const text = typeof input === "string" ? input : JSON.stringify(input);
  const code = await runBridge(text, dependencies);
  return { records, code };
}

describe("runBridge", () => {
  test("emits hello, then one result, and exits zero", async () => {
    const { records, code } = await run(request());
    expect(code).toBe(0);
    expect(records[0]).toEqual({ type: "hello", bridge_protocol: BRIDGE_PROTOCOL });
    expect(records.at(-1)).toEqual({ type: "result", kind: "reply", content: "Hi." });
    expect(records.filter((record) => record.type === "result")).toHaveLength(1);
  });

  test("turns a handoff-capable answer into a handoff result", async () => {
    const { records, code } = await run(request({ allow_handoff: true }), {
      async createHost() {
        return fakeHost({
          async prompt() {
            return '{"type":"handoff","capability":"research","task":"Find it."}';
          },
        });
      },
    });
    expect(code).toBe(0);
    expect(records.at(-1)).toEqual({
      type: "result",
      kind: "handoff",
      capability: "research",
      task: "Find it.",
    });
  });

  test("forwards trace events between hello and the result", async () => {
    const trace: TraceEvent = {
      server: "files",
      tool: "search",
      status: "running",
      phase: "called",
    };
    const { records } = await run(request(), {
      async createHost() {
        return fakeHost({
          async prompt(_text, onTrace) {
            onTrace(trace);
            return "done";
          },
        });
      },
    });
    expect(records).toEqual([
      { type: "hello", bridge_protocol: BRIDGE_PROTOCOL },
      { type: "event", ...trace },
      { type: "result", kind: "reply", content: "done" },
    ]);
  });

  test("passes the model, the composed system prompt and the MCP grants to the host", async () => {
    let seen:
      | { model: string; system: string; mcpServers: readonly unknown[] }
      | undefined;
    const mcpServers = [
      {
        name: "files",
        command: "npx",
        args: ["-y", "readonly-files"],
        env_vars: [],
        tools: ["lookup"],
      },
    ];
    await run(request({ instructions: "Be terse.", allow_handoff: true, tool_ids: ["files/lookup"], mcp_servers: mcpServers }), {
      async createHost(options) {
        seen = options;
        return fakeHost();
      },
    });
    expect(seen?.model).toBe("opencode/big-model");
    expect(seen?.system).toContain("Be terse.");
    expect(seen?.system).toContain("handoff");
    expect(seen?.mcpServers).toEqual(mcpServers);
  });

  test("refuses a version outside the window before any host work", async () => {
    let created = false;
    const { records, code } = await run(request({ bridge_protocol: 1 }), {
      async createHost() {
        created = true;
        return fakeHost();
      },
    });
    expect(created).toBe(false);
    expect(code).toBe(1);
    expect(records).toEqual([
      { type: "hello", bridge_protocol: BRIDGE_PROTOCOL },
      {
        type: "error",
        kind: "bridge_version",
        message: "The request bridge_protocol 1 is outside the compatible window 2 <= v < 3.",
      },
    ]);
  });

  test("reports an invalid request as a request error", async () => {
    const notJson = await run("{not json");
    expect(notJson.code).toBe(1);
    expect(notJson.records.at(-1)).toEqual({
      type: "error",
      kind: "request",
      message: "The request is not valid JSON.",
    });

    const unknown = await run(request({ extra: true }));
    expect(unknown.records.at(-1)).toMatchObject({ kind: "request" });
  });

  test("reports a policy that is not in force as a provider error", async () => {
    const { records, code } = await run(request(), {
      async createHost() {
        return fakeHost({
          async verifyPolicy() {
            throw new BridgeError("provider", "The OpenCode tool policy is not in force: shell.");
          },
        });
      },
    });
    expect(code).toBe(1);
    expect(records.at(-1)).toEqual({
      type: "error",
      kind: "provider",
      message: "The OpenCode tool policy is not in force: shell.",
    });
  });

  test("collapses an SDK failure into a labelled, safe message", async () => {
    const failure = Object.assign(new Error("401 Invalid key sk-secret-value"), {
      _tag: "ProviderError",
    });
    const { records, code } = await run(request(), {
      async createHost() {
        return fakeHost({
          async prompt() {
            throw failure;
          },
        });
      },
    });
    expect(code).toBe(1);
    expect(records.at(-1)).toEqual({
      type: "error",
      kind: "provider",
      message: "The OpenCode host failed the model request (ProviderError).",
    });
    expect(JSON.stringify(records)).not.toContain("sk-secret-value");
  });

  test("a host that cannot start fails the turn", async () => {
    const { records, code } = await run(request(), {
      async createHost() {
        throw new Error("no host");
      },
    });
    expect(code).toBe(1);
    expect(records.at(-1)).toMatchObject({ type: "error", kind: "provider" });
  });

  test("closes the host on both paths", async () => {
    let closed = false;
    await run(request(), {
      async createHost() {
        return fakeHost({
          async close() {
            closed = true;
          },
        });
      },
    });
    expect(closed).toBe(true);

    closed = false;
    await run(request(), {
      async createHost() {
        return fakeHost({
          async prompt() {
            throw new Error("boom");
          },
          async close() {
            closed = true;
          },
        });
      },
    });
    expect(closed).toBe(true);
  });

  test("answers a models request with one catalog result and exits zero", async () => {
    const models = [
      {
        id: "opencode/big-model",
        label: "Big Model",
        is_default: true,
        default_effort: "",
        efforts: ["low", "high"],
      },
    ];
    let created = false;
    const { records, code } = await run(
      { bridge_protocol: BRIDGE_PROTOCOL, operation: "models" },
      {
        async listModels() {
          return models;
        },
        async createHost() {
          created = true;
          return fakeHost();
        },
      },
    );
    expect(created).toBe(false);
    expect(code).toBe(0);
    expect(records).toEqual([
      { type: "hello", bridge_protocol: BRIDGE_PROTOCOL },
      { type: "result", kind: "models", models },
    ]);
    expect(records.filter((record) => record.type === "result")).toHaveLength(1);
  });

  test("fails a models request with one error record", async () => {
    const { records, code } = await run(
      { bridge_protocol: BRIDGE_PROTOCOL, operation: "models" },
      {
        async listModels() {
          throw new BridgeError("provider", "The model catalog is unavailable.");
        },
      },
    );
    expect(code).toBe(1);
    expect(records.at(-1)).toEqual({
      type: "error",
      kind: "provider",
      message: "The model catalog is unavailable.",
    });
  });

  test("refuses an incompatible version before reading the catalog", async () => {
    let listed = false;
    const { records, code } = await run(
      { bridge_protocol: 1, operation: "models" },
      {
        async listModels() {
          listed = true;
          return [];
        },
      },
    );
    expect(listed).toBe(false);
    expect(code).toBe(1);
    expect(records.at(-1)).toMatchObject({ type: "error", kind: "bridge_version" });
  });

  test("answers an integrations request with one descriptor list", async () => {
    const integrations = [
      {
        id: "openai",
        name: "OpenAI",
        methods: [
          { id: "chatgpt-headless", type: "oauth", label: "ChatGPT Pro/Plus (headless)" },
        ],
        connected: false,
      },
    ];
    let created = false;
    const { records, code } = await run(
      { bridge_protocol: BRIDGE_PROTOCOL, operation: "integrations" },
      {
        async listIntegrations() {
          return integrations;
        },
        async createHost() {
          created = true;
          return fakeHost();
        },
      },
    );
    expect(created).toBe(false);
    expect(code).toBe(0);
    expect(records).toEqual([
      { type: "hello", bridge_protocol: BRIDGE_PROTOCOL },
      { type: "result", kind: "integrations", integrations },
    ]);
  });

  test("connect emits the sign-in details, then the connected result", async () => {
    const seen: { options?: unknown; hooks?: ConnectionHooks } = {};
    const { records, code } = await run(
      { bridge_protocol: BRIDGE_PROTOCOL, operation: "connect", integration: "openai" },
      {
        async connect(options, hooks) {
          seen.options = options;
          seen.hooks = hooks;
          hooks.onAttempt({
            attempt_id: "attempt-1",
            url: "https://auth.openai.com/codex/device",
            instructions: "Enter code: ABCD",
            mode: "auto",
          });
          return { integration: "openai", method: "chatgpt-headless" };
        },
      },
    );
    expect(seen.options).toEqual({ integration: "openai" });
    expect(code).toBe(0);
    expect(records).toEqual([
      { type: "hello", bridge_protocol: BRIDGE_PROTOCOL },
      {
        type: "oauth",
        attempt_id: "attempt-1",
        url: "https://auth.openai.com/codex/device",
        instructions: "Enter code: ABCD",
        mode: "auto",
      },
      { type: "result", kind: "connected", integration: "openai", method: "chatgpt-headless" },
    ]);
  });

  test("connect in code mode reads the code line from the bridge's caller", async () => {
    let requested = 0;
    const { code } = await run(
      { bridge_protocol: BRIDGE_PROTOCOL, operation: "connect", integration: "openai" },
      {
        async readCode() {
          requested += 1;
          return "the-code";
        },
        async connect(_options, hooks) {
          const attempt = {
            attempt_id: "attempt-1",
            url: "https://example.test",
            instructions: "Paste the code",
            mode: "code" as const,
          };
          hooks.onAttempt(attempt);
          const received = await hooks.waitForCode(attempt);
          expect(received).toBe("the-code");
          return { integration: "openai", method: "example" };
        },
      },
    );
    expect(requested).toBe(1);
    expect(code).toBe(0);
  });

  test("fails a connect attempt with one error record", async () => {
    const { records, code } = await run(
      { bridge_protocol: BRIDGE_PROTOCOL, operation: "connect", integration: "openai" },
      {
        async connect() {
          throw new BridgeError("provider", "The provider connection failed. Try again.");
        },
      },
    );
    expect(code).toBe(1);
    expect(records.at(-1)).toEqual({
      type: "error",
      kind: "provider",
      message: "The provider connection failed. Try again.",
    });
    expect(records.filter((record) => record.type === "result")).toHaveLength(0);
  });
});

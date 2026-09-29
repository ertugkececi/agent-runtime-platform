import { describe, expect, test } from "bun:test";

import {
  BRIDGE_PROTOCOL,
  BridgeError,
  diagnosticLine,
  hello,
  isCompatible,
  parseRequest,
} from "../src/protocol";

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

function mcpServer(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    name: "files",
    command: "npx",
    args: ["-y", "readonly-files"],
    env_vars: ["FILES_TOKEN"],
    tools: ["lookup"],
    ...overrides,
  };
}

/** Parse a request that must be a turn, so its fields are typed. */
function parseTurn(value: unknown): import("../src/protocol").TurnRequest {
  const parsed = parseRequest(value);
  if (parsed.operation !== undefined && parsed.operation !== "turn") {
    throw new Error("expected a turn request");
  }
  return parsed;
}

describe("hello", () => {
  test("announces the protocol this host speaks", () => {
    expect(hello()).toEqual({ type: "hello", bridge_protocol: BRIDGE_PROTOCOL });
  });
});

describe("isCompatible", () => {
  test("accepts the window 2 <= v < 3", () => {
    expect(isCompatible(2)).toBe(true);
  });

  test("refuses versions outside the window in either direction", () => {
    for (const version of [0, 1, 3, -1, 2.5]) {
      expect(isCompatible(version)).toBe(false);
    }
  });
});

describe("diagnosticLine", () => {
  test("writes the severity and nothing an SDK entry carries", () => {
    const entry = {
      level: "warn",
      message: "tool shell failed with sk-secret-value",
      attributes: { api_key: "sk-secret-value", input: { command: "rm -rf /" } },
    };
    expect(diagnosticLine(entry)).toBe("opencode-host warn\n");
  });
});

describe("parseRequest", () => {
  test("accepts a complete request", () => {
    const parsed = parseTurn(request());
    expect(parsed.bridge_protocol).toBe(BRIDGE_PROTOCOL);
    expect(parsed.model).toBe("opencode/big-model");
    expect(parsed.instructions).toBe("Be helpful.");
    expect(parsed.history).toEqual([{ role: "user", content: "Hello." }]);
    expect(parsed.allow_handoff).toBe(false);
    expect(parsed.tool_ids).toEqual([]);
    expect(parsed.mcp_servers).toBeUndefined();
    expect(parsed.remote_capabilities).toBeUndefined();
    expect(parsed.reasoning_effort).toBeUndefined();
  });

  test("carries the optional fields when present", () => {
    const parsed = parseTurn(
      request({
        allow_handoff: true,
        remote_capabilities: ["research"],
        reasoning_effort: "high",
      }),
    );
    expect(parsed.allow_handoff).toBe(true);
    expect(parsed.remote_capabilities).toEqual(["research"]);
    expect(parsed.reasoning_effort).toBe("high");
  });

  test("carries an approved MCP server with its grant", () => {
    const parsed = parseTurn(
      request({ tool_ids: ["files/lookup"], mcp_servers: [mcpServer()] }),
    );
    expect(parsed.mcp_servers).toEqual([
      {
        name: "files",
        command: "npx",
        args: ["-y", "readonly-files"],
        env_vars: ["FILES_TOKEN"],
        tools: ["lookup"],
      },
    ]);
  });

  test("refuses a grant without its MCP servers, and servers without a grant", () => {
    expect(() => parseRequest(request({ tool_ids: ["files/lookup"] }))).toThrow(
      "must carry their mcp_servers",
    );
    expect(() =>
      parseTurn(request({ mcp_servers: [mcpServer()] })),
    ).toThrow("must grant at least one tool_id");
  });

  test("refuses an MCP server with unknown fields or an empty grant", () => {
    expect(() =>
      parseTurn(
        request({
          tool_ids: ["files/lookup"],
          mcp_servers: [mcpServer({ extra: true })],
        }),
      ),
    ).toThrow("unknown field 'extra'");
    expect(() =>
      parseTurn(
        request({ tool_ids: ["files/lookup"], mcp_servers: [mcpServer({ tools: [] })] }),
      ),
    ).toThrow("at least one tool");
  });

  test("refuses unknown fields instead of ignoring them", () => {
    expect(() => parseRequest(request({ extra: 1 }))).toThrow(BridgeError);
  });

  test("refuses a missing field", () => {
    const incomplete = request();
    delete incomplete["instructions"];
    expect(() => parseRequest(incomplete)).toThrow("must be a string");
  });

  test("refuses wrong types", () => {
    expect(() => parseRequest(request({ bridge_protocol: "1" }))).toThrow(
      "must be an integer",
    );
    expect(() => parseRequest(request({ allow_handoff: "yes" }))).toThrow(
      "must be a boolean",
    );
    expect(() => parseRequest(request({ tool_ids: [1] }))).toThrow(
      "must be an array of strings",
    );
  });

  test("refuses a history entry that is not a user or assistant message", () => {
    expect(() => parseRequest(request({ history: [{ role: "system", content: "x" }] }))).toThrow(
      "role 'user' or 'assistant'",
    );
    expect(() => parseRequest(request({ history: [{ role: "user", content: 4 }] }))).toThrow(
      "string content",
    );
    expect(() => parseRequest(request({ history: ["hello"] }))).toThrow("must be an object");
  });

  test("refuses a request that is not an object", () => {
    for (const value of [null, 4, "request", ["request"]]) {
      expect(() => parseRequest(value)).toThrow("must be a JSON object");
    }
  });

  test("parses catalog and integration requests as their own shapes", () => {
    expect(parseRequest({ bridge_protocol: BRIDGE_PROTOCOL, operation: "models" })).toEqual({
      bridge_protocol: BRIDGE_PROTOCOL,
      operation: "models",
    });
    expect(parseRequest({ bridge_protocol: BRIDGE_PROTOCOL, operation: "integrations" })).toEqual({
      bridge_protocol: BRIDGE_PROTOCOL,
      operation: "integrations",
    });
    expect(parseRequest(request({ operation: "turn" })).operation).toBe("turn");
  });

  test("parses a connect request and refuses a blank integration", () => {
    expect(
      parseRequest({
        bridge_protocol: BRIDGE_PROTOCOL,
        operation: "connect",
        integration: "openai",
        method: "chatgpt-headless",
        label: "work",
      }),
    ).toEqual({
      bridge_protocol: BRIDGE_PROTOCOL,
      operation: "connect",
      integration: "openai",
      method: "chatgpt-headless",
      label: "work",
    });
    expect(() =>
      parseRequest({ bridge_protocol: BRIDGE_PROTOCOL, operation: "connect", integration: " " }),
    ).toThrow("must not be blank");
  });

  test("refuses an unknown operation", () => {
    expect(() => parseRequest({ bridge_protocol: BRIDGE_PROTOCOL, operation: "catalog" })).toThrow(
      "'turn', 'models', 'integrations' or 'connect'",
    );
  });

  test("refuses turn fields on a catalog request", () => {
    expect(() =>
      parseRequest({
        bridge_protocol: BRIDGE_PROTOCOL,
        operation: "models",
        model: "opencode/big-model",
      }),
    ).toThrow("unknown field 'model'");
    expect(() =>
      parseRequest({
        bridge_protocol: BRIDGE_PROTOCOL,
        operation: "integrations",
        integration: "openai",
      }),
    ).toThrow("unknown field 'integration'");
  });

  test("refuses a catalog request without a protocol version", () => {
    expect(() => parseRequest({ operation: "models" })).toThrow("must be an integer");
  });

  test("never echoes field values in error messages", () => {
    try {
      parseRequest(request({ model: 42, history: [{ role: "user", content: "secret-token" }] }));
      throw new Error("expected a refusal");
    } catch (error) {
      expect(String(error)).not.toContain("secret-token");
    }
  });
});

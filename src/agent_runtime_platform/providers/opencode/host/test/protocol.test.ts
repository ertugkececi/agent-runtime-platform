import { describe, expect, test } from "bun:test";

import {
  BRIDGE_PROTOCOL,
  BridgeError,
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

/** Parse a request that must be a turn, so its fields are typed. */
function parseTurn(value: unknown): import("../src/protocol").TurnRequest {
  const parsed = parseRequest(value);
  if (parsed.operation === "models") throw new Error("expected a turn request");
  return parsed;
}

describe("hello", () => {
  test("announces the protocol this host speaks", () => {
    expect(hello()).toEqual({ type: "hello", bridge_protocol: 1 });
  });
});

describe("isCompatible", () => {
  test("accepts the window 1 <= v < 2", () => {
    expect(isCompatible(1)).toBe(true);
  });

  test("refuses versions outside the window in either direction", () => {
    for (const version of [0, 2, 3, -1, 1.5]) {
      expect(isCompatible(version)).toBe(false);
    }
  });
});

describe("parseRequest", () => {
  test("accepts a complete request", () => {
    const parsed = parseTurn(request());
    expect(parsed.bridge_protocol).toBe(1);
    expect(parsed.model).toBe("opencode/big-model");
    expect(parsed.instructions).toBe("Be helpful.");
    expect(parsed.history).toEqual([{ role: "user", content: "Hello." }]);
    expect(parsed.allow_handoff).toBe(false);
    expect(parsed.tool_ids).toEqual([]);
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

  test("accepts non-empty tool_ids; refusing them is the bridge's job", () => {
    expect(parseTurn(request({ tool_ids: ["fixture/lookup"] })).tool_ids).toEqual([
      "fixture/lookup",
    ]);
  });

  test("refuses a request that is not an object", () => {
    for (const value of [null, 4, "request", ["request"]]) {
      expect(() => parseRequest(value)).toThrow("must be a JSON object");
    }
  });

  test("parses a models request as its own shape", () => {
    expect(parseRequest({ bridge_protocol: 1, operation: "models" })).toEqual({
      bridge_protocol: 1,
      operation: "models",
    });
    expect(parseRequest(request({ operation: "turn" })).operation).toBe("turn");
  });

  test("refuses an unknown operation", () => {
    expect(() => parseRequest({ bridge_protocol: 1, operation: "catalog" })).toThrow(
      "'turn' or 'models'",
    );
  });

  test("refuses turn fields on a models request", () => {
    expect(() =>
      parseRequest({ bridge_protocol: 1, operation: "models", model: "opencode/big-model" }),
    ).toThrow("unknown field 'model'");
  });

  test("refuses a models request without a protocol version", () => {
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

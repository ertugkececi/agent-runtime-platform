import { describe, expect, test } from "bun:test";

import type { BridgeRequest } from "../src/protocol";
import {
  buildPromptText,
  buildSystemPrompt,
  extractFinalText,
  interpretFinalText,
  turnOutcome,
} from "../src/turn";

function request(overrides: Partial<BridgeRequest> = {}): BridgeRequest {
  return {
    bridge_protocol: 1,
    model: "opencode/big-model",
    instructions: "Follow the agent instructions.",
    history: [
      { role: "user", content: "What is a queue?" },
      { role: "assistant", content: "A line of work." },
      { role: "user", content: "Summarize the queue module." },
    ],
    allow_handoff: false,
    tool_ids: [],
    ...overrides,
  };
}

describe("buildSystemPrompt", () => {
  test("carries the request instructions", () => {
    const prompt = buildSystemPrompt(request());
    expect(prompt).toContain("Follow the agent instructions.");
    expect(prompt).toContain("Do not use tools");
  });

  test("describes the handoff contract only when handoffs are allowed", () => {
    expect(buildSystemPrompt(request())).not.toContain("handoff");

    const withHandoff = buildSystemPrompt(
      request({ allow_handoff: true, remote_capabilities: ["research", "summary"] }),
    );
    expect(withHandoff).toContain('"type":"handoff"');
    expect(withHandoff).toContain("research, summary");
  });
});

describe("buildPromptText", () => {
  test("sends the whole history as one JSON message", () => {
    const text = buildPromptText(request());
    expect(text).toContain("conversation history");
    expect(text).toContain('"What is a queue?"');
    expect(text.trim().endsWith("]")).toBe(true);
  });
});

describe("interpretFinalText", () => {
  test("keeps plain text as the reply", () => {
    expect(interpretFinalText("The queue lives in queue.py.", false)).toEqual({
      kind: "reply",
      content: "The queue lives in queue.py.",
    });
  });

  test("accepts a reply envelope", () => {
    expect(
      interpretFinalText('{"type":"reply","content":"Hello there."}', true),
    ).toEqual({ kind: "reply", content: "Hello there." });
  });

  test("accepts a handoff envelope only when handoffs are allowed", () => {
    const text = '{"type":"handoff","capability":" research ","task":" Find the schema. "}';
    expect(interpretFinalText(text, true)).toEqual({
      kind: "handoff",
      capability: "research",
      task: "Find the schema.",
    });
    expect(interpretFinalText(text, false)).toEqual({ kind: "reply", content: text });
  });

  test("an incomplete handoff stays a reply", () => {
    const text = '{"type":"handoff","capability":"","task":""}';
    expect(interpretFinalText(text, true)).toEqual({ kind: "reply", content: text });
  });

  test("malformed JSON stays a reply", () => {
    const text = "{not json";
    expect(interpretFinalText(text, true)).toEqual({ kind: "reply", content: text });
    expect(interpretFinalText("[1,2,3]", true)).toEqual({ kind: "reply", content: "[1,2,3]" });
  });

  test("an empty answer is an empty reply", () => {
    expect(interpretFinalText(undefined, true)).toEqual({ kind: "reply", content: "" });
  });
});

describe("turnOutcome", () => {
  test("reads the newest idle message", () => {
    expect(
      turnOutcome([
        { type: "idle", time: { created: 9 }, outcome: "failed" },
        { type: "idle", time: { created: 3 }, outcome: "succeeded" },
      ]),
    ).toBe("failed");
    expect(turnOutcome([{ type: "idle", time: { created: 1 }, outcome: "interrupted" }])).toBe(
      "interrupted",
    );
    expect(turnOutcome([{ type: "idle", time: { created: 1 }, outcome: "succeeded" }])).toBe(
      "succeeded",
    );
  });

  test("an idle message without a known outcome counts as failed", () => {
    expect(turnOutcome([{ type: "idle", time: { created: 1 } }])).toBe("failed");
    expect(turnOutcome([{ type: "idle", time: { created: 1 }, outcome: "weird" }])).toBe("failed");
  });

  test("no idle message means no turn result", () => {
    expect(turnOutcome([])).toBeUndefined();
    expect(turnOutcome([{ type: "assistant", time: { created: 1 } }])).toBeUndefined();
  });
});

describe("extractFinalText", () => {
  test("takes the newest assistant message with text", () => {
    const messages = [
      { type: "assistant", time: { created: 2 }, content: [{ type: "text", text: "second" }] },
      { type: "assistant", time: { created: 1 }, content: [{ type: "text", text: "first" }] },
      { type: "user", time: { created: 3 }, content: [{ type: "text", text: "user" }] },
    ];
    expect(extractFinalText(messages)).toBe("second");
  });

  test("joins the text parts of one message", () => {
    const messages = [
      {
        type: "assistant",
        time: { created: 1 },
        content: [
          { type: "reasoning", text: "thinking" },
          { type: "text", text: "Hello " },
          { type: "text", text: "world." },
        ],
      },
    ];
    expect(extractFinalText(messages)).toBe("Hello world.");
  });

  test("returns undefined when no assistant text exists", () => {
    expect(extractFinalText([])).toBeUndefined();
    expect(
      extractFinalText([{ type: "assistant", time: { created: 1 }, content: [] }]),
    ).toBeUndefined();
    expect(extractFinalText([{ type: "idle", time: { created: 2 } }])).toBeUndefined();
  });
});

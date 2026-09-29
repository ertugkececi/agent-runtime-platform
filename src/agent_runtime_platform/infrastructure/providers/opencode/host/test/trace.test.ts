import { describe, expect, test } from "bun:test";

import { createTraceMapper, type SdkEventLike } from "../src/trace";

const TOOLS = [
  { id: "shell" },
  { id: "files_search", options: { namespace: "files" } },
];

function event(type: string, data: Record<string, unknown>): SdkEventLike {
  return { type, data };
}

describe("createTraceMapper", () => {
  test("maps a tool call to a running trace record", () => {
    const map = createTraceMapper(() => TOOLS);
    map(event("session.tool.input.started", { id: "call-1", name: "shell" }));
    expect(map(event("session.tool.called", { id: "call-1", input: { command: "rm -rf /" } }))).toEqual(
      { server: "opencode", tool: "shell", status: "running", phase: "called" },
    );
  });

  test("completes and fails the call with the remembered name", () => {
    const map = createTraceMapper(() => TOOLS);
    map(event("session.tool.input.started", { id: "call-1", name: "shell" }));
    expect(map(event("session.tool.success", { id: "call-1", content: ["secret"] }))).toEqual({
      server: "opencode",
      tool: "shell",
      status: "completed",
      phase: "completed",
    });
    expect(map(event("session.tool.failed", { id: "call-1" }))).toEqual({
      server: "opencode",
      tool: "shell",
      status: "error",
      phase: "failed",
    });
  });

  test("splits an MCP tool into its server and tool names", () => {
    const map = createTraceMapper(() => TOOLS);
    map(event("session.tool.input.started", { id: "call-2", name: "files_search" }));
    expect(map(event("session.tool.called", { id: "call-2" }))).toEqual({
      server: "files",
      tool: "search",
      status: "running",
      phase: "called",
    });
  });

  test("carries nothing but the trace fields", () => {
    const map = createTraceMapper(() => TOOLS);
    map(event("session.tool.input.started", { id: "call-1", name: "shell" }));
    const record = map(event("session.tool.called", { id: "call-1", input: { command: "secret" } }));
    expect(Object.keys(record ?? {}).sort()).toEqual([
      "phase",
      "server",
      "status",
      "tool",
    ]);
    expect(JSON.stringify(record)).not.toContain("secret");
  });

  test("ignores unknown events and events without a known call", () => {
    const map = createTraceMapper(() => TOOLS);
    expect(map(event("session.text.started", { sessionID: "ses_1" }))).toBeUndefined();
    expect(map(event("session.tool.called", { id: "call-1" }))).toBeUndefined();
    expect(map(event("session.tool.called", {}))).toBeUndefined();
    expect(map({ type: "project.updated" })).toBeUndefined();
  });
});

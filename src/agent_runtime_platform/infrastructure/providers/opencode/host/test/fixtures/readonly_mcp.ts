/**
 * A dependency-free stdio MCP server for the host tests.
 *
 * It speaks newline-delimited JSON-RPC (the MCP stdio transport) and exposes
 * one read-only-looking tool. It never reaches the network and never touches
 * the model service.
 */

interface JsonRpcRequest {
  jsonrpc?: unknown;
  id?: unknown;
  method?: unknown;
  params?: unknown;
}

const TOOLS = [
  {
    name: "lookup",
    description: "Return a fixed value.",
    inputSchema: {
      type: "object",
      properties: { topic: { type: "string" } },
    },
    annotations: { readOnlyHint: true },
  },
];

function send(message: unknown): void {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function handle(line: string): void {
  let request: JsonRpcRequest;
  try {
    request = JSON.parse(line) as JsonRpcRequest;
  } catch {
    return;
  }
  const id = request.id;
  switch (request.method) {
    case "initialize":
      send({
        jsonrpc: "2.0",
        id,
        result: {
          protocolVersion: "2025-06-18",
          capabilities: { tools: {} },
          serverInfo: { name: "readonly-fixture", version: "0.0.0" },
        },
      });
      return;
    case "notifications/initialized":
      return;
    case "tools/list":
      send({ jsonrpc: "2.0", id, result: { tools: TOOLS } });
      return;
    case "tools/call":
      send({
        jsonrpc: "2.0",
        id,
        result: { content: [{ type: "text", text: "fixture result" }], isError: false },
      });
      return;
    case "ping":
      send({ jsonrpc: "2.0", id, result: {} });
      return;
    default:
      if (id !== undefined) {
        send({ jsonrpc: "2.0", id, error: { code: -32601, message: "Method not found" } });
      }
  }
}

let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk: string) => {
  buffer += chunk;
  let index = buffer.indexOf("\n");
  while (index >= 0) {
    const line = buffer.slice(0, index).trim();
    buffer = buffer.slice(index + 1);
    if (line) handle(line);
    index = buffer.indexOf("\n");
  }
});

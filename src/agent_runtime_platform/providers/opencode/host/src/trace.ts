/**
 * Tool trace records for the event stream.
 *
 * An event record carries `{server, tool, status, phase}` and nothing else:
 * never tool arguments, tool results, prompts, or model output. That is the
 * whole point of the record, so the mapping here reads names and lifecycle
 * only.
 */

export interface TraceEvent {
  server: string;
  tool: string;
  status: string;
  phase: string;
}

export interface ToolDescriptor {
  readonly id: string;
  readonly options?: { readonly namespace?: string };
}

/** The shape of a session event this module understands. */
export interface SdkEventLike {
  type: string;
  data?: unknown;
}

const BUILT_IN_SERVER = "opencode";

/**
 * Map SDK session events to trace records.
 *
 * Tool success and failure events identify a call by id, so the tool name is
 * remembered from the input event that precedes the call. Unknown event types
 * are ignored; that is the only exception to "never silently adapt".
 */
export function createTraceMapper(
  tools: () => readonly ToolDescriptor[],
): (event: SdkEventLike) => TraceEvent | undefined {
  const names = new Map<string, string>();

  return (event) => {
    const data = asRecord(event.data);
    if (!data) return undefined;
    const id = data["id"];

    switch (event.type) {
      case "session.tool.input.started": {
        if (typeof id === "string" && typeof data["name"] === "string") {
          names.set(id, data["name"]);
        }
        return undefined;
      }
      case "session.tool.called":
        return trace(id, names, tools(), "running", "called");
      case "session.tool.success":
        return trace(id, names, tools(), "completed", "completed");
      case "session.tool.failed":
        return trace(id, names, tools(), "error", "failed");
      default:
        return undefined;
    }
  };
}

function trace(
  id: unknown,
  names: Map<string, string>,
  tools: readonly ToolDescriptor[],
  status: string,
  phase: string,
): TraceEvent | undefined {
  const toolID = typeof id === "string" ? names.get(id) : undefined;
  if (toolID === undefined) return undefined;
  const { server, tool } = splitName(toolID, tools);
  return { server, tool, status, phase };
}

/** Split a tool id into the display server and tool names. */
function splitName(
  toolID: string,
  tools: readonly ToolDescriptor[],
): { server: string; tool: string } {
  const namespace = tools.find((tool) => tool.id === toolID)?.options?.namespace;
  if (namespace === undefined || namespace === "") {
    return { server: BUILT_IN_SERVER, tool: toolID };
  }
  const prefix = `${namespace}_`;
  return {
    server: namespace,
    tool: toolID.startsWith(prefix) ? toolID.slice(prefix.length) : toolID,
  };
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

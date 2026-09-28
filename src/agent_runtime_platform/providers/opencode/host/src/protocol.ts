/**
 * The OpenCode bridge wire protocol.
 *
 * This is the host side of the one boundary between the Python adapter and this
 * package: one JSON request on stdin, NDJSON records on stdout. The authority
 * is `docs/opencode-bridge-contract.md`; nothing here may drift from it.
 *
 * The module deliberately imports nothing from the OpenCode SDK. It is the
 * part of the host that can be reasoned about, and tested, without a model
 * service or an embedded server.
 */

/** The protocol version this host speaks. */
export const BRIDGE_PROTOCOL = 1;

/** A version `v` is compatible when `COMPATIBLE_MIN <= v < COMPATIBLE_MAX_EXCLUSIVE`. */
export const COMPATIBLE_MIN = 1;
export const COMPATIBLE_MAX_EXCLUSIVE = 2;

export type ErrorKind =
  | "bridge_version"
  | "tool_refused"
  | "request"
  | "provider"
  | "timeout";

export interface HistoryMessage {
  role: "user" | "assistant";
  content: string;
}

/** A call either runs one model turn (the default) or reads the model catalog. */
export interface TurnRequest {
  /** Explicitly names the default operation; the adapter normally omits it. */
  operation?: "turn";
  bridge_protocol: number;
  model: string;
  instructions: string;
  history: HistoryMessage[];
  allow_handoff: boolean;
  tool_ids: string[];
  remote_capabilities?: string[];
  /**
   * Effort hint for models that have one. OpenCode selects effort through
   * model variants, which a model reference names as `provider/model#variant`;
   * the field is therefore parsed for the contract but not applied here.
   */
  reasoning_effort?: string;
}

export interface ModelsRequest {
  bridge_protocol: number;
  operation: "models";
}

export type BridgeRequest = TurnRequest | ModelsRequest;

/**
 * One model choice, in the shape the HTTP model catalogs share.
 *
 * `id` is the reference a turn request uses (`provider/model`), so a catalog
 * entry can be sent back as `model` unchanged. `default_effort` is empty when
 * the model has no default variant; `efforts` are the model's variant ids.
 */
export interface CatalogModel {
  id: string;
  label: string;
  is_default: boolean;
  default_effort: string;
  efforts: string[];
}

export interface HelloRecord {
  type: "hello";
  bridge_protocol: number;
}

export interface EventRecord {
  type: "event";
  server: string;
  tool: string;
  status: string;
  phase: string;
}

export interface ReplyRecord {
  type: "result";
  kind: "reply";
  content: string;
}

export interface HandoffRecord {
  type: "result";
  kind: "handoff";
  capability: string;
  task: string;
}

export interface ModelsRecord {
  type: "result";
  kind: "models";
  models: CatalogModel[];
}

export type ResultRecord = ReplyRecord | HandoffRecord | ModelsRecord;

export interface ErrorRecord {
  type: "error";
  kind: ErrorKind;
  message: string;
}

export type BridgeRecord = HelloRecord | EventRecord | ResultRecord | ErrorRecord;

/**
 * An error whose message is written by this host, not echoed from the SDK.
 *
 * The bridge reports `kind` and `message` verbatim. Anything that is not a
 * `BridgeError` is an unexpected failure and is collapsed into a generic
 * message, so no SDK text, credential, or stack trace can reach the adapter.
 */
export class BridgeError extends Error {
  constructor(
    readonly kind: ErrorKind,
    message: string,
  ) {
    super(message);
    this.name = "BridgeError";
  }
}

const TURN_FIELDS = new Set([
  "operation",
  "bridge_protocol",
  "model",
  "instructions",
  "history",
  "allow_handoff",
  "tool_ids",
  "remote_capabilities",
  "reasoning_effort",
]);

const MODELS_FIELDS = new Set(["bridge_protocol", "operation"]);

export function hello(): HelloRecord {
  return { type: "hello", bridge_protocol: BRIDGE_PROTOCOL };
}

export function isCompatible(version: number): boolean {
  return Number.isInteger(version) && version >= COMPATIBLE_MIN && version < COMPATIBLE_MAX_EXCLUSIVE;
}

/**
 * Parse and validate the one request object.
 *
 * Unknown fields are refused rather than ignored, as the contract requires.
 * Messages never echo field values: a request may carry conversation content.
 */
export function parseRequest(value: unknown): BridgeRequest {
  if (!isRecord(value)) {
    throw new BridgeError("request", "The request must be a JSON object.");
  }
  const operation = value["operation"];
  if (operation !== undefined && operation !== "turn" && operation !== "models") {
    throw new BridgeError(
      "request",
      "The request field 'operation' must be 'turn' or 'models'.",
    );
  }
  if (operation === "models") {
    for (const field of Object.keys(value)) {
      if (!MODELS_FIELDS.has(field)) {
        throw new BridgeError("request", `The request has an unknown field '${field}'.`);
      }
    }
    return {
      bridge_protocol: requireInteger(value, "bridge_protocol"),
      operation: "models",
    };
  }
  for (const field of Object.keys(value)) {
    if (!TURN_FIELDS.has(field)) {
      throw new BridgeError("request", `The request has an unknown field '${field}'.`);
    }
  }

  const bridgeProtocol = requireInteger(value, "bridge_protocol");
  const model = requireString(value, "model");
  const instructions = requireString(value, "instructions");
  const history = requireHistory(value);
  const allowHandoff = requireBoolean(value, "allow_handoff");
  const toolIds = requireStringArray(value, "tool_ids");
  const remoteCapabilities =
    value.remote_capabilities === undefined
      ? undefined
      : requireStringArray(value, "remote_capabilities");
  const reasoningEffort =
    value.reasoning_effort === undefined ? undefined : requireString(value, "reasoning_effort");

  return {
    ...(operation === "turn" ? { operation: "turn" as const } : {}),
    bridge_protocol: bridgeProtocol,
    model,
    instructions,
    history,
    allow_handoff: allowHandoff,
    tool_ids: toolIds,
    ...(remoteCapabilities === undefined ? {} : { remote_capabilities: remoteCapabilities }),
    ...(reasoningEffort === undefined ? {} : { reasoning_effort: reasoningEffort }),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireInteger(source: Record<string, unknown>, field: string): number {
  const value = source[field];
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new BridgeError("request", `The request field '${field}' must be an integer.`);
  }
  return value;
}

function requireString(source: Record<string, unknown>, field: string): string {
  const value = source[field];
  if (typeof value !== "string") {
    throw new BridgeError("request", `The request field '${field}' must be a string.`);
  }
  return value;
}

function requireBoolean(source: Record<string, unknown>, field: string): boolean {
  const value = source[field];
  if (typeof value !== "boolean") {
    throw new BridgeError("request", `The request field '${field}' must be a boolean.`);
  }
  return value;
}

function requireStringArray(source: Record<string, unknown>, field: string): string[] {
  const value = source[field];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new BridgeError("request", `The request field '${field}' must be an array of strings.`);
  }
  return value as string[];
}

function requireHistory(source: Record<string, unknown>): HistoryMessage[] {
  const value = source["history"];
  if (!Array.isArray(value)) {
    throw new BridgeError("request", "The request field 'history' must be an array.");
  }
  return value.map((item, index) => {
    if (!isRecord(item)) {
      throw new BridgeError("request", `The history entry at index ${index} must be an object.`);
    }
    const role = item["role"];
    if (role !== "user" && role !== "assistant") {
      throw new BridgeError(
        "request",
        `The history entry at index ${index} must have role 'user' or 'assistant'.`,
      );
    }
    const content = item["content"];
    if (typeof content !== "string") {
      throw new BridgeError(
        "request",
        `The history entry at index ${index} must have a string content.`,
      );
    }
    return { role, content };
  });
}

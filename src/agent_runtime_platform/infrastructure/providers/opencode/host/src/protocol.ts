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
export const BRIDGE_PROTOCOL = 2;

/** A version `v` is compatible when `COMPATIBLE_MIN <= v < COMPATIBLE_MAX_EXCLUSIVE`. */
export const COMPATIBLE_MIN = 2;
export const COMPATIBLE_MAX_EXCLUSIVE = 3;

export type ErrorKind = "bridge_version" | "request" | "provider" | "timeout";

export interface HistoryMessage {
  role: "user" | "assistant";
  content: string;
}

/**
 * One administrator-approved MCP server, reduced to what the host needs.
 *
 * `env_vars` are names only; the host resolves their values from its own
 * process environment, so no credential ever travels on the wire. `tools`
 * lists the granted tool names of that server; the host allows exactly those
 * and denies every other action.
 */
export interface McpServerRequest {
  name: string;
  command: string;
  args: string[];
  cwd?: string;
  env_vars: string[];
  tools: string[];
}

/** A call runs one model turn (the default), reads a catalog, or connects an integration. */
export interface TurnRequest {
  /** Explicitly names the default operation; the adapter normally omits it. */
  operation?: "turn";
  bridge_protocol: number;
  model: string;
  instructions: string;
  history: HistoryMessage[];
  allow_handoff: boolean;
  tool_ids: string[];
  /** The approved MCP servers behind `tool_ids`; empty when no tool is granted. */
  mcp_servers?: McpServerRequest[];
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

export interface IntegrationsRequest {
  bridge_protocol: number;
  operation: "integrations";
}

/** Start one interactive provider connection and wait until it completes. */
export interface ConnectRequest {
  bridge_protocol: number;
  operation: "connect";
  /** Integration id, for example "openai". */
  integration: string;
  /** OAuth method id; when omitted the host prefers the headless method. */
  method?: string;
  /** Optional account label the credential is stored under. */
  label?: string;
}

export type BridgeRequest = TurnRequest | ModelsRequest | IntegrationsRequest | ConnectRequest;

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

/** One sign-in method an integration offers, reduced to display fields. */
export interface IntegrationMethodDescriptor {
  id: string;
  type: string;
  label: string;
}

/** One integration and its methods, nothing else. */
export interface IntegrationDescriptor {
  id: string;
  name: string;
  methods: IntegrationMethodDescriptor[];
  connected: boolean;
}

/** The sign-in details the caller shows the human, and nothing else. */
export interface OauthAttempt {
  attempt_id: string;
  url: string;
  instructions: string;
  mode: "auto" | "code";
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

export interface OauthRecord extends OauthAttempt {
  type: "oauth";
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

export interface IntegrationsRecord {
  type: "result";
  kind: "integrations";
  integrations: IntegrationDescriptor[];
}

export interface ConnectedRecord {
  type: "result";
  kind: "connected";
  integration: string;
  method: string;
}

export type ResultRecord =
  | ReplyRecord
  | HandoffRecord
  | ModelsRecord
  | IntegrationsRecord
  | ConnectedRecord;

export interface ErrorRecord {
  type: "error";
  kind: ErrorKind;
  message: string;
}

export type BridgeRecord = HelloRecord | EventRecord | OauthRecord | ResultRecord | ErrorRecord;

/** The part of an SDK log entry the host is willing to see. */
export interface DiagnosticEntry {
  readonly level: string;
}

/**
 * The one stderr line an SDK log entry produces: its severity, and nothing
 * else. SDK messages and attributes may carry tool arguments, tool results,
 * prompts, or credentials, and the bridge never logs any of those.
 */
export function diagnosticLine(entry: DiagnosticEntry): string {
  return `opencode-host ${entry.level}\n`;
}

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
  "mcp_servers",
  "remote_capabilities",
  "reasoning_effort",
]);

const OPERATION_FIELDS = new Set(["bridge_protocol", "operation"]);

const CONNECT_FIELDS = new Set(["bridge_protocol", "operation", "integration", "method", "label"]);

const MCP_SERVER_FIELDS = new Set(["name", "command", "args", "cwd", "env_vars", "tools"]);

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
  if (
    operation !== undefined &&
    operation !== "turn" &&
    operation !== "models" &&
    operation !== "integrations" &&
    operation !== "connect"
  ) {
    throw new BridgeError(
      "request",
      "The request field 'operation' must be 'turn', 'models', 'integrations' or 'connect'.",
    );
  }
  if (operation === "models" || operation === "integrations") {
    rejectUnknownFields(value, OPERATION_FIELDS);
    return {
      bridge_protocol: requireInteger(value, "bridge_protocol"),
      operation,
    };
  }
  if (operation === "connect") {
    rejectUnknownFields(value, CONNECT_FIELDS);
    const method = optionalString(value, "method");
    const label = optionalString(value, "label");
    const integration = requireString(value, "integration").trim();
    if (!integration) {
      throw new BridgeError("request", "The request field 'integration' must not be blank.");
    }
    return {
      bridge_protocol: requireInteger(value, "bridge_protocol"),
      operation: "connect",
      integration,
      ...(method === undefined ? {} : { method }),
      ...(label === undefined ? {} : { label }),
    };
  }
  rejectUnknownFields(value, TURN_FIELDS);

  const bridgeProtocol = requireInteger(value, "bridge_protocol");
  const model = requireString(value, "model");
  const instructions = requireString(value, "instructions");
  const history = requireHistory(value);
  const allowHandoff = requireBoolean(value, "allow_handoff");
  const toolIds = requireStringArray(value, "tool_ids");
  const mcpServers = requireMcpServers(value);
  if (toolIds.length > 0 && mcpServers.length === 0) {
    throw new BridgeError("request", "A turn that grants tool_ids must carry their mcp_servers.");
  }
  if (toolIds.length === 0 && mcpServers.length > 0) {
    throw new BridgeError("request", "A turn that carries mcp_servers must grant at least one tool_id.");
  }
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
    ...(mcpServers.length === 0 ? {} : { mcp_servers: mcpServers }),
    ...(remoteCapabilities === undefined ? {} : { remote_capabilities: remoteCapabilities }),
    ...(reasoningEffort === undefined ? {} : { reasoning_effort: reasoningEffort }),
  };
}

function rejectUnknownFields(source: Record<string, unknown>, known: ReadonlySet<string>): void {
  for (const field of Object.keys(source)) {
    if (!known.has(field)) {
      throw new BridgeError("request", `The request has an unknown field '${field}'.`);
    }
  }
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

function optionalString(source: Record<string, unknown>, field: string): string | undefined {
  const value = source[field];
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !value.trim()) {
    throw new BridgeError("request", `The request field '${field}' must be a non-empty string.`);
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

function requireMcpServers(source: Record<string, unknown>): McpServerRequest[] {
  const value = source["mcp_servers"];
  if (value === undefined) return [];
  if (!Array.isArray(value)) {
    throw new BridgeError("request", "The request field 'mcp_servers' must be an array.");
  }
  const seen = new Set<string>();
  return value.map((item, index) => {
    if (!isRecord(item)) {
      throw new BridgeError("request", `The mcp_servers entry at index ${index} must be an object.`);
    }
    rejectUnknownFields(item, MCP_SERVER_FIELDS);
    const name = requireString(item, "name").trim();
    const command = requireString(item, "command").trim();
    const args = requireStringArray(item, "args");
    const envVars = requireStringArray(item, "env_vars");
    const tools = requireStringArray(item, "tools");
    const cwd = item["cwd"] === undefined ? undefined : requireString(item, "cwd");
    if (!name || !command) {
      throw new BridgeError(
        "request",
        `The mcp_servers entry at index ${index} requires a non-empty 'name' and 'command'.`,
      );
    }
    if (tools.length === 0 || tools.some((tool) => !tool.trim())) {
      throw new BridgeError(
        "request",
        `The mcp_servers entry '${name}' must grant at least one tool.`,
      );
    }
    if (seen.has(name)) {
      throw new BridgeError("request", `The mcp_servers entry '${name}' is duplicated.`);
    }
    seen.add(name);
    return {
      name,
      command,
      args,
      env_vars: envVars,
      tools,
      ...(cwd === undefined ? {} : { cwd }),
    };
  });
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

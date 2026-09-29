/**
 * The embedded OpenCode host, driven for one call.
 *
 * One process, one call: start the SDK host, boot a location in a private
 * working directory, then either run one turn (create a session, make sure the
 * tool policy is in force, prompt, read the final assistant text), read the
 * model catalog, or connect a provider integration. Nothing is shared between
 * calls; the process exits when the call does. The host keeps its SQLite
 * database on disk under the data root the adapter points at `XDG_DATA_HOME`
 * (`opencode.db`), because the embedded SDK would otherwise default to an
 * in-memory database and lose every credential with the process.
 */

import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { OpenCode, Model } from "@opencode/sdk";

import type { TurnHost, TurnHostOptions } from "./bridge";
import { configDirectory, dataDirectory } from "./isolation";
import { createToolPolicyPlugin } from "./plugin";
import {
  BRIDGE_AGENT_ID,
  POLICY_PLUGIN_ID,
  assertToolPolicy,
  serverToolActions,
  toolPolicy,
} from "./policy";
import { BridgeError, diagnosticLine, type CatalogModel, type McpServerRequest } from "./protocol";
import type { ToolDescriptor } from "./trace";
import { createTraceMapper } from "./trace";
import { extractFinalText, turnOutcome } from "./turn";

const READINESS_ATTEMPTS = 50;
const READINESS_DELAY_MS = 200;

/** The system prompt of the host that only reads catalogs. */
const CATALOG_SYSTEM = "The bridge host agent. It reads the catalog and runs no turn.";

/** One private OpenCode host: the SDK instance, its roots, and its cleanup. */
export interface PrivateHost {
  readonly opencode: Awaited<ReturnType<typeof OpenCode.create>>;
  readonly workDirectory: string;
  readonly toolCatalog: () => readonly ToolDescriptor[];
  readonly refreshToolCatalog: () => Promise<readonly ToolDescriptor[]>;
  close(): Promise<void>;
}

/** The permission actions the turn's MCP grants resolve to. */
export function grantedToolActions(servers: readonly McpServerRequest[]): string[] {
  return servers.flatMap((server) => serverToolActions(server.name, server.tools));
}

/**
 * Start the SDK host in a private root with the tool policy plugin installed.
 *
 * The config directory is private, so the host never loads the invoking user's
 * OpenCode configuration, and the policy plugin is in place before any work.
 * MCP servers are registered with exactly the granted tools; their credentials
 * arrive through this process's environment, never through the request.
 */
async function openPrivateHost(
  system: string,
  servers: readonly McpServerRequest[],
): Promise<PrivateHost> {
  const privateRoot = mkdtempSync(join(tmpdir(), "agent-runtime-opencode-"));
  const workDirectory = join(privateRoot, "work");
  const privateConfig = configDirectory(process.env, privateRoot);
  const dataRoot = dataDirectory(process.env, privateRoot);
  const databasePath = join(dataRoot, "opencode.db");
  mkdirSync(workDirectory, { recursive: true, mode: 0o700 });
  mkdirSync(dataRoot, { recursive: true, mode: 0o700 });
  const previousCwd = process.cwd();
  process.chdir(workDirectory);

  const allowedActions = grantedToolActions(servers);
  const policy = toolPolicy(allowedActions);
  const { plugin, catalog, refresh } = createToolPolicyPlugin(allowedActions);
  let opencode: Awaited<ReturnType<typeof OpenCode.create>>;
  try {
    opencode = await OpenCode.create({
      // The embedded SDK defaults to an in-memory database; the bridge keeps
      // its database on disk under the persistent data root instead, so a
      // provider sign-in survives the process that stored it.
      database: { path: databasePath },
      // The grant list is the policy; see policy.ts. The agent carries the
      // rules too, and `verifyPolicy` below re-checks the effective ones.
      config: {
        directory: privateConfig,
        project: false,
        content: JSON.stringify({
          permissions: policy,
          agents: {
            [BRIDGE_AGENT_ID]: {
              description: "The bridge host agent. It runs with the granted read-only MCP tools.",
              mode: "primary",
              system,
              permissions: policy,
            },
          },
          ...mcpServerConfig(servers),
        }),
      },
      // The bundled model catalog is enough to resolve a model; no network
      // fetch, and no update or share traffic from a bridge process.
      models: { fetch: false },
      log: {
        level: "warn",
        // Only the severity reaches stderr, never the SDK's message or
        // attributes: those may carry prompts, tool arguments, or results.
        emit: (entry) => process.stderr.write(diagnosticLine(entry)),
      },
      plugins: [plugin],
    });
  } catch (error) {
    process.chdir(previousCwd);
    rmSync(privateRoot, { recursive: true, force: true });
    throw error;
  }

  return {
    opencode,
    workDirectory,
    toolCatalog: catalog,
    refreshToolCatalog: refresh,
    async close() {
      try {
        await opencode.close();
      } finally {
        process.chdir(previousCwd);
        rmSync(privateRoot, { recursive: true, force: true });
      }
    },
  };
}

/**
 * Start one private host and boot its location; the caller owns `close`.
 *
 * The location boots asynchronously and the host plugins are applied during
 * that boot, so every caller waits for the bridge agent before it asks the
 * host anything.
 */
export async function bootPrivateHost(
  system: string,
  servers: readonly McpServerRequest[] = [],
): Promise<PrivateHost> {
  const host = await openPrivateHost(system, servers);
  try {
    await createSession(host);
    await waitForAgent(host.opencode);
    return host;
  } catch (error) {
    await host.close().catch(() => {});
    throw error;
  }
}

async function createSession(
  host: PrivateHost,
  model?: { providerID: string; id: string; variant?: string },
) {
  return host.opencode.sessions.create({
    location: { directory: host.workDirectory },
    agent: BRIDGE_AGENT_ID,
    ...(model === undefined ? {} : { model }),
  });
}

/** The `mcp` section of the private config: local servers, values from the environment. */
function mcpServerConfig(servers: readonly McpServerRequest[]): Record<string, unknown> {
  if (servers.length === 0) return {};
  const entries: Record<string, unknown> = {};
  for (const server of servers) {
    const environment: Record<string, string> = {};
    for (const name of server.env_vars) {
      const value = process.env[name];
      if (value !== undefined && value !== "") environment[name] = value;
    }
    entries[server.name] = {
      type: "local",
      command: [server.command, ...server.args],
      ...(server.cwd === undefined ? {} : { cwd: server.cwd }),
      ...(Object.keys(environment).length === 0 ? {} : { environment }),
    };
  }
  return { mcp: { servers: entries } };
}

export async function startTurnHost(options: TurnHostOptions): Promise<TurnHost> {
  const model = parseModelReference(options.model);
  const host = await openPrivateHost(options.system, options.mcpServers);

  try {
    const allowedActions = grantedToolActions(options.mcpServers);
    const session = await createSession(host, {
      providerID: model.providerID,
      id: model.id,
      ...(model.variant === undefined ? {} : { variant: model.variant }),
    });
    await waitForAgent(host.opencode);
    // A location boots asynchronously; MCP servers may still be connecting,
    // so the granted tools are awaited before the policy is re-checked.
    await waitForGrantedTools(host, allowedActions);

    return {
      async verifyPolicy() {
        const plugins = await host.opencode.plugin.list();
        if (!plugins.data.some((entry) => entry.id === POLICY_PLUGIN_ID)) {
          throw new BridgeError(
            "provider",
            "The OpenCode tool policy plugin is not active. Refusing to start the turn.",
          );
        }
        const agent = await host.opencode.agent.get({ agentID: BRIDGE_AGENT_ID });
        assertToolPolicy(agent.data.permissions, allowedActions);
      },

      async prompt(text, onTrace) {
        const controller = new AbortController();
        const mapper = createTraceMapper(host.toolCatalog);
        const subscription = (async () => {
          try {
            for await (const event of host.opencode.events.subscribe({ signal: controller.signal })) {
              if (!belongsToSession(event, session.id)) continue;
              const trace = mapper(event);
              if (trace) onTrace(trace);
            }
          } catch {
            // The stream ends with the turn; the result comes from the session.
          }
        })();
        try {
          await host.opencode.sessions.prompt({ sessionID: session.id, text });
          await host.opencode.sessions.wait({ sessionID: session.id });
        } finally {
          controller.abort();
          await subscription;
        }
        const messages = await host.opencode.message.list({ sessionID: session.id });
        const outcome = turnOutcome(messages.data);
        if (outcome !== "succeeded") {
          throw new BridgeError(
            "provider",
            outcome === "interrupted"
              ? "The OpenCode model request was interrupted."
              : outcome === "failed"
                ? "The OpenCode model request failed."
                : "The OpenCode session ended without a turn result.",
          );
        }
        return extractFinalText(messages.data);
      },

      async close() {
        await host.close();
      },
    };
  } catch (error) {
    await host.close().catch(() => {});
    throw error;
  }
}

/**
 * Read the model catalog from one short-lived host.
 *
 * The catalog is the bundled snapshot; a provider without credentials simply
 * has no models, so an empty list is a valid answer. Entries use the shared
 * HTTP catalog shape, and `id` is the reference a turn request sends as
 * `model`.
 */
export async function listModels(): Promise<CatalogModel[]> {
  const host = await bootPrivateHost(CATALOG_SYSTEM);
  try {
    const [models, fallback] = await Promise.all([
      host.opencode.model.list(),
      host.opencode.model.default(),
    ]);
    const defaultModel = fallback.data;
    return models.data.map((model) => ({
      id: `${model.providerID}/${model.id}`,
      label: model.name,
      is_default:
        defaultModel?.providerID === model.providerID && defaultModel.id === model.id,
      default_effort: "",
      efforts: model.variants.map((variant) => variant.id),
    }));
  } finally {
    // A failed close must not replace the answer the call produced.
    await host.close().catch(() => {});
  }
}

function parseModelReference(reference: string): {
  providerID: string;
  id: string;
  variant?: string;
} {
  try {
    return Model.Ref.parse(reference);
  } catch {
    throw new BridgeError(
      "request",
      "The request field 'model' must name a provider and a model, for example 'opencode/big-model'.",
    );
  }
}

async function waitForAgent(
  opencode: Awaited<ReturnType<typeof OpenCode.create>>,
): Promise<void> {
  for (let attempt = 0; attempt < READINESS_ATTEMPTS; attempt++) {
    try {
      await opencode.agent.get({ agentID: BRIDGE_AGENT_ID });
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, READINESS_DELAY_MS));
    }
  }
  throw new BridgeError(
    "provider",
    `The OpenCode host did not register the '${BRIDGE_AGENT_ID}' agent in time.`,
  );
}

/** Wait until every granted tool is registered, so the model can call it. */
async function waitForGrantedTools(
  host: PrivateHost,
  allowedActions: readonly string[],
): Promise<void> {
  if (allowedActions.length === 0) return;
  for (let attempt = 0; attempt < READINESS_ATTEMPTS; attempt++) {
    const registered = new Set((await host.refreshToolCatalog()).map((tool) => tool.id));
    if (allowedActions.every((action) => registered.has(action))) return;
    await new Promise((resolve) => setTimeout(resolve, READINESS_DELAY_MS));
  }
  throw new BridgeError(
    "provider",
    "The OpenCode host did not register the granted MCP tools in time.",
  );
}

function belongsToSession(event: { data?: unknown }, sessionID: string): boolean {
  const data = event.data;
  if (typeof data !== "object" || data === null || Array.isArray(data)) return false;
  return (data as { sessionID?: unknown }).sessionID === sessionID;
}

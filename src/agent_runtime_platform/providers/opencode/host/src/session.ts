/**
 * The embedded OpenCode host, driven for one call.
 *
 * One process, one call: start the SDK host, boot a location in a private
 * working directory, then either run one turn (create a session, make sure the
 * tool policy is in force, prompt, read the final assistant text) or read the
 * model catalog. Nothing is shared between calls; the process exits when the
 * call does.
 */

import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { OpenCode, Model } from "@opencode/sdk";

import type { TurnHost, TurnHostOptions } from "./bridge";
import { configDirectory } from "./isolation";
import { createToolPolicyPlugin } from "./plugin";
import {
  BRIDGE_AGENT_ID,
  POLICY_PLUGIN_ID,
  TOOL_POLICY,
  assertToolPolicy,
  type PermissionRule,
} from "./policy";
import { BridgeError, diagnosticLine, type CatalogModel } from "./protocol";
import type { ToolDescriptor } from "./trace";
import { createTraceMapper } from "./trace";
import { extractFinalText, turnOutcome } from "./turn";

const READINESS_ATTEMPTS = 50;
const READINESS_DELAY_MS = 200;

/** The system prompt of the host that only reads the model catalog. */
const CATALOG_SYSTEM = "The bridge host agent. It reads the model catalog and runs no turn.";

/** One private OpenCode host: the SDK instance, its roots, and its cleanup. */
interface PrivateHost {
  readonly opencode: Awaited<ReturnType<typeof OpenCode.create>>;
  readonly workDirectory: string;
  readonly toolCatalog: () => readonly ToolDescriptor[];
  close(): Promise<void>;
}

/**
 * Start the SDK host in a private root with the tool policy plugin installed.
 *
 * The config directory is private, so the host never loads the invoking user's
 * OpenCode configuration, and the policy plugin is in place before any work.
 */
async function openPrivateHost(system: string): Promise<PrivateHost> {
  const privateRoot = mkdtempSync(join(tmpdir(), "agent-runtime-opencode-"));
  const workDirectory = join(privateRoot, "work");
  const privateConfig = configDirectory(process.env, privateRoot);
  mkdirSync(workDirectory, { recursive: true, mode: 0o700 });
  const previousCwd = process.cwd();
  process.chdir(workDirectory);

  const { plugin, catalog } = createToolPolicyPlugin();
  let opencode: Awaited<ReturnType<typeof OpenCode.create>>;
  try {
    opencode = await OpenCode.create({
      // An empty tool catalog is the policy; see policy.ts. The agent carries
      // the rules too, and `verifyPolicy` below re-checks the effective ones.
      config: {
        directory: privateConfig,
        project: false,
        content: JSON.stringify({
          permissions: TOOL_POLICY,
          agents: {
            [BRIDGE_AGENT_ID]: {
              description: "The bridge host agent. It runs without tools.",
              mode: "primary",
              system,
              permissions: TOOL_POLICY,
            },
          },
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

export async function startTurnHost(options: TurnHostOptions): Promise<TurnHost> {
  const model = parseModelReference(options.model);
  const host = await openPrivateHost(options.system);

  try {
    const session = await host.opencode.sessions.create({
      location: { directory: host.workDirectory },
      agent: BRIDGE_AGENT_ID,
      model: {
        providerID: model.providerID,
        id: model.id,
        ...(model.variant === undefined ? {} : { variant: model.variant }),
      },
    });

    // A location boots asynchronously, and the host plugins (including this
    // policy) are applied during that boot. Wait for the session's agent
    // before re-checking the policy.
    await waitForAgent(host.opencode);

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
        assertToolPolicy(agent.data.permissions as readonly PermissionRule[]);
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
  const host = await openPrivateHost(CATALOG_SYSTEM);
  try {
    await host.opencode.sessions.create({
      location: { directory: host.workDirectory },
      agent: BRIDGE_AGENT_ID,
    });
    await waitForAgent(host.opencode);
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

function belongsToSession(event: { data?: unknown }, sessionID: string): boolean {
  const data = event.data;
  if (typeof data !== "object" || data === null || Array.isArray(data)) return false;
  return (data as { sessionID?: unknown }).sessionID === sessionID;
}
